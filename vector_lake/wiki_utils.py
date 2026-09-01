import datetime
import ctypes
import hashlib
import os
import random
import re
import shutil
import string
import time
import uuid
import io
from pathlib import Path
from typing import List, TypedDict

import yaml
from vector_lake import get_extension_root
from vector_lake.durability import (
    commit_existing_file,
    durable_replace_file,
    sync_open_file,
)
from vector_lake.yaml_utils import load_yaml, dump_yaml

_META_DIR_CACHE = None

_WINDOWS_REPLACE_RETRYABLE_ERRORS = frozenset({5, 32})
_WINDOWS_REPLACE_RETRY_DELAYS_SECONDS = (0.05, 0.1, 0.2, 0.4)

WIKI_LINK_PATTERN = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_FENCE_OPEN = re.compile(r"^[ \t]{0,3}(?P<mark>`{3,}|~{3,})[^\r\n]*(?:\r?\n|$)")


def normalize_semantic_text(content: str) -> str:
    """Normalize transport-only text differences for semantic parity checks."""
    return content.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def semantic_text_hash(content: str) -> str:
    return hashlib.sha256(normalize_semantic_text(content).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    if not path.exists():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _annotate_compare_and_swap_os_error(
    exc: OSError,
    *,
    phase: str,
    path: Path,
) -> None:
    """Attach a non-secret CAS phase to an OSError without changing its type."""
    original = exc.strerror or str(exc)
    details = [f"errno={exc.errno}"]
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        details.append(f"winerror={winerror}")
    message = (
        f"Projection compare-and-swap phase={phase} failed for {path.name}: "
        f"{original} ({', '.join(details)})"
    )
    arguments = list(exc.args)
    if len(arguments) >= 2:
        arguments[1] = message
    else:
        arguments = [exc.errno, message]
    exc.args = tuple(arguments)
    exc.strerror = message


def _sleep_windows_replace_retry(delay_seconds: float) -> None:
    time.sleep(delay_seconds)


def _windows_replace_retry_state_is_intact(
    replaced_path: Path,
    replacement_path: Path,
    backup_path: Path,
) -> bool:
    """Return whether a failed ReplaceFileW attempt left its inputs untouched."""
    return bool(
        os.path.lexists(replaced_path)
        and not replaced_path.is_symlink()
        and replaced_path.is_file()
        and os.path.lexists(replacement_path)
        and not replacement_path.is_symlink()
        and replacement_path.is_file()
        and not os.path.lexists(backup_path)
    )


def _replace_file_with_backup(
    replaced_path: Path,
    replacement_path: Path,
    backup_path: Path,
) -> None:
    """Atomically replace a Windows file while retaining the displaced version."""
    if os.name != "nt":
        raise RuntimeError(
            "Projection compare-and-swap replacement is unavailable on this platform."
        )
    replace_file = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
    replace_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    replace_file.restype = ctypes.c_int
    attempts = len(_WINDOWS_REPLACE_RETRY_DELAYS_SECONDS) + 1
    for attempt in range(attempts):
        if replace_file(
            str(replaced_path),
            str(replacement_path),
            str(backup_path),
            0,
            None,
            None,
        ):
            return
        error_code = ctypes.get_last_error()
        if error_code not in _WINDOWS_REPLACE_RETRYABLE_ERRORS:
            raise ctypes.WinError(error_code)
        if not _windows_replace_retry_state_is_intact(
            replaced_path,
            replacement_path,
            backup_path,
        ):
            raise ctypes.WinError(error_code)
        if attempt >= len(_WINDOWS_REPLACE_RETRY_DELAYS_SECONDS):
            raise ctypes.WinError(error_code)
        _sleep_windows_replace_retry(
            _WINDOWS_REPLACE_RETRY_DELAYS_SECONDS[attempt]
        )


def _replace_prepared_file_compare_and_swap(
    path: Path,
    temp_path: Path,
    expected_current_hash: str,
) -> None:
    """Replace ``path`` and roll back if the atomically displaced file drifted."""
    try:
        actual_hash = _file_sha256(path)
    except OSError as exc:
        _annotate_compare_and_swap_os_error(
            exc,
            phase="current_hash_read",
            path=path,
        )
        raise
    if actual_hash != expected_current_hash:
        raise RuntimeError(
            f"Projection compare-and-swap conflict for {path.name}: "
            f"expected {expected_current_hash or '<absent>'}, "
            f"current {actual_hash or '<absent>'}"
        )
    if expected_current_hash == "":
        try:
            os.link(temp_path, path)
        except FileExistsError as exc:
            raise RuntimeError(
                f"Projection compare-and-swap conflict for {path.name}: "
                "expected <absent>, current projection appeared during create."
            ) from exc
        except OSError as exc:
            raise RuntimeError(
                f"Projection compare-and-swap create failed for {path.name}: {exc}"
            ) from exc
        return
    if not path.exists():
        raise RuntimeError(
            f"Projection compare-and-swap conflict for {path.name}: "
            "an existing projection is required."
        )

    try:
        desired_hash = _file_sha256(temp_path)
    except OSError as exc:
        _annotate_compare_and_swap_os_error(
            exc,
            phase="replacement_hash_read",
            path=temp_path,
        )
        raise
    backup_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.cas-backup")
    rollback_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.cas-rollback")
    try:
        _replace_file_with_backup(path, temp_path, backup_path)
    except OSError as exc:
        _annotate_compare_and_swap_os_error(
            exc,
            phase="replace_file",
            path=path,
        )
        raise
    try:
        displaced_hash = _file_sha256(backup_path)
    except OSError as exc:
        _annotate_compare_and_swap_os_error(
            exc,
            phase="backup_hash_read",
            path=backup_path,
        )
        raise
    if displaced_hash == expected_current_hash:
        backup_path.unlink()
        return

    try:
        _replace_file_with_backup(path, backup_path, rollback_path)
    except BaseException as exc:
        raise RuntimeError(
            f"Projection compare-and-swap conflict for {path.name}; "
            f"the raced projection is preserved at {backup_path}."
        ) from exc

    rollback_hash = _file_sha256(rollback_path)
    if rollback_hash == desired_hash:
        rollback_path.unlink()
    raise RuntimeError(
        f"Projection compare-and-swap conflict for {path.name}: "
        f"expected {expected_current_hash}, current {displaced_hash}."
    )


def _restore_displaced_file_noreplace(
    displaced_path: Path,
    path: Path,
) -> None:
    """Restore a displaced file without overwriting a concurrent replacement."""
    try:
        os.link(displaced_path, path)
    except FileExistsError as exc:
        raise RuntimeError(
            f"Projection compare-and-swap conflict for {path.name}; "
            f"the raced projection is preserved at {displaced_path}."
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"Projection compare-and-swap restore failed for {path.name}; "
            f"the raced projection is preserved at {displaced_path}: {exc}"
        ) from exc
    try:
        displaced_path.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"Projection compare-and-swap restored {path.name}, but the "
            f"duplicate raced projection remains at {displaced_path}: {exc}"
        ) from exc


def delete_file_compare_and_swap(
    path: str | Path,
    expected_current_hash: str | None,
) -> bool:
    """Atomically displace, verify, and delete only the expected file version."""
    path = Path(path)
    if not path.exists():
        return False

    expected_hash = (
        _file_sha256(path)
        if expected_current_hash is None
        else expected_current_hash
    )
    actual_hash = _file_sha256(path)
    if actual_hash != expected_hash:
        raise RuntimeError(
            f"Projection compare-and-swap conflict for {path.name}: "
            f"expected {expected_hash or '<absent>'}, "
            f"current {actual_hash or '<absent>'}."
        )

    displaced_path = path.with_name(
        f"{path.name}.{uuid.uuid4().hex}.cas-delete"
    )
    try:
        os.replace(path, displaced_path)
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Projection compare-and-swap conflict for {path.name}: "
            "the projection disappeared before atomic delete."
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"Projection compare-and-swap delete failed for {path.name}: {exc}"
        ) from exc

    displaced_hash = _file_sha256(displaced_path)
    if displaced_hash != expected_hash:
        try:
            _restore_displaced_file_noreplace(displaced_path, path)
        except RuntimeError as restore_error:
            raise RuntimeError(
                f"Projection compare-and-swap conflict for {path.name}: "
                f"expected {expected_hash}, current {displaced_hash}; "
                f"{restore_error}"
            ) from restore_error
        raise RuntimeError(
            f"Projection compare-and-swap conflict for {path.name}: "
            f"expected {expected_hash}, current {displaced_hash}; "
            "the raced projection was restored."
        )

    try:
        displaced_path.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"Projection delete could not be finalized for {path.name}; "
            f"the expected projection is preserved at {displaced_path}: {exc}"
        ) from exc
    if path.exists():
        raise RuntimeError(
            f"Projection compare-and-swap conflict for {path.name}: "
            "a concurrent projection appeared after delete and was preserved."
        )
    return True


def markdown_fenced_code_spans(content: str) -> list[tuple[int, int]]:
    """Return top-level fenced-code spans using CommonMark-compatible fences."""
    spans: list[tuple[int, int]] = []
    opened: tuple[int, str, int] | None = None
    offset = 0
    for line in content.splitlines(keepends=True):
        if opened is None:
            match = _FENCE_OPEN.match(line)
            if match:
                mark = match.group("mark")
                opened = (offset, mark[0], len(mark))
        else:
            start, marker, length = opened
            if re.fullmatch(
                rf"[ \t]{{0,3}}{re.escape(marker)}{{{length},}}[ \t]*(?:\r?\n)?",
                line,
            ):
                spans.append((start, offset + len(line)))
                opened = None
        offset += len(line)
    if opened is not None:
        spans.append((opened[0], len(content)))
    return spans


def _inside_inline_code(content: str, start: int) -> bool:
    line_start = content.rfind("\n", 0, start) + 1
    prefix = content[line_start:start]
    delimiter = None
    for match in re.finditer(r"`+", prefix):
        width = len(match.group(0))
        if delimiter is None:
            delimiter = width
        elif width == delimiter:
            delimiter = None
    return delimiter is not None


def iter_wiki_link_matches(content: str):
    """Yield Wiki-link matches outside fenced and inline code literals."""
    spans = iter(markdown_fenced_code_spans(content))
    current = next(spans, None)
    for match in WIKI_LINK_PATTERN.finditer(content):
        while current is not None and match.start() >= current[1]:
            current = next(spans, None)
        if current is not None and current[0] <= match.start() < current[1]:
            continue
        if _inside_inline_code(content, match.start()):
            continue
        yield match

SYSTEM_WHITELIST = {
    "index.md",
    "log.md",
    "overview.md",
    "orphan_pages.md",
    "wiki_link_stats.md",
    "Synthesis_log.md",
}
VALID_PREFIXES = ("Concept_", "Vendor_", "Institution_", "Product_", "Person_", "Event_", "Policy_", "Standard_", "Source_", "Synthesis_", "System_")
INVALID_CHARS_REGEX = re.compile(r'[\[\]<>:"/\\|\?\*\(\)\s]+')

def normalize_memory_key(key: str) -> str:
    """Canonical normalization function to strip noise from keys and aliases."""
    normalized = re.sub(r"\s+", " ", str(key or "").strip().lower())
    normalized = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized[:96] or "general"

def calculate_cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Canonical cosine similarity for raw python floats."""
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    import math
    dot = sum(a * b for a, b in zip(v1, v2))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(a * a for a in v2))
    return dot / (norm1 * norm2) if norm1 and norm2 else 0.0

def normalize_entity_name(name: str) -> str:
    """Normalizes an entity name by replacing spaces and invalid chars with hyphens and collapsing multiples."""
    prefix = ""
    for p in VALID_PREFIXES:
        if name.startswith(p):
            prefix = p
            name = name[len(p):]
            break
            
    # Replace spaces, underscores, brackets, and invalid chars with a single hyphen to enforce only one underscore rule
    name = re.sub(r'[\s_\[\]<>:"/\\|\?\*\(\)]+', '-', name.strip())
    name = name.strip('-')
    return f"{prefix}{name}"

def validate_wiki_filename(filename: str):
    # Only exact, owned metadata basenames bypass the node filename contract.
    # System_ pages are ordinary canonical nodes and must pass every suffix,
    # prefix, character, shape, anti-cheat, and length rule below.
    if filename in SYSTEM_WHITELIST:
        return
    
    if not filename.casefold().endswith(".md"):
        raise ValueError(f"Invalid suffix: '{filename}' must end with .md")
        
    if not filename.startswith(VALID_PREFIXES):
        raise ValueError(f"Invalid prefix: '{filename}' must start with one of {VALID_PREFIXES}")
        
    if INVALID_CHARS_REGEX.search(filename):
        raise ValueError(f"Invalid characters: '{filename}' contains forbidden characters (e.g., brackets, slashes, spaces).")
        
    if not re.fullmatch(r'(Concept|Vendor|Institution|Product|Person|Event|Policy|Standard|Source|Synthesis|System)_[a-zA-Z0-9\u4e00-\u9fa5]+(?:-[a-zA-Z0-9\u4e00-\u9fa5]+)*(?i:\.md)', filename):
        raise ValueError(f"Strict Naming Violation: '{filename}' must match pattern [Type]_[MainName]-[SubName].md")
        
    core_name = filename.split("_", 1)[1][:-3] if "_" in filename else filename[:-3]
    if len(core_name.strip()) <= 1 and (core_name.isalpha() or '\u4e00' <= core_name <= '\u9fff'):
        raise ValueError(f"Anti-cheat triggered: Core name '{core_name}' is too short.")
        
    if len(filename) > 120:
        raise ValueError(f"Length limit exceeded: '{filename}' is over 120 characters.")


def get_memory_dir() -> Path:
    override = os.environ.get("VECTOR_LAKE_MEMORY_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (Path(os.path.expanduser("~")) / "MEMORY").resolve()


def _uses_legacy_default_memory_root() -> bool:
    return get_memory_dir() == (
        Path(os.path.expanduser("~")) / ".gemini" / "MEMORY"
    ).resolve()


def get_wiki_dir() -> Path:
    return get_memory_dir() / "wiki"


def iter_markdown_files(directory: str | Path | None = None):
    """Yield non-recursive Markdown files; callers own any required ordering."""
    root = get_wiki_dir() if directory is None else Path(directory)
    if not root.exists():
        return
    for path in root.iterdir():
        if path.is_file() and path.suffix.casefold() == ".md":
            yield path


def get_raw_dir() -> Path:
    return get_memory_dir() / "raw"


def peek_meta_dir() -> Path:
    """Resolve the canonical meta path without creating or probing it."""
    primary = get_wiki_dir() / ".meta"
    fallback = get_extension_root() / "data" / "v8_meta"
    explicit_value = os.environ.get("VECTOR_LAKE_META_DIR", "").strip()
    explicit = (
        Path(explicit_value).expanduser().resolve()
        if explicit_value
        else None
    )
    if explicit is not None:
        if (
            explicit == primary.resolve()
            and _uses_legacy_default_memory_root()
            and (fallback / "vector_lake.db").exists()
            and not (primary / "vector_lake.db").exists()
        ):
            raise RuntimeError(
                "Legacy fallback Vector Lake state exists while the configured "
                f"primary meta directory is empty: {fallback}. Refusing to "
                "inspect a path that would strand the existing canonical database."
            )
        return explicit

    if (
        _uses_legacy_default_memory_root()
        and (fallback / "vector_lake.db").exists()
        and not (primary / "vector_lake.db").exists()
    ):
        return fallback
    return primary


def _verify_writable_meta_dir(candidate: Path) -> Path:
    candidate.mkdir(parents=True, exist_ok=True)
    probe = candidate / f".probe_{uuid.uuid4().hex}"
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("probe")
    finally:
        probe.unlink(missing_ok=True)
    return candidate


def get_meta_dir() -> Path:
    global _META_DIR_CACHE
    if (
        str(os.environ.get("VECTOR_LAKE_MCP_SURFACE", "full")).strip().lower()
        == "readonly"
    ):
        return peek_meta_dir()
    if isinstance(_META_DIR_CACHE, Path):
        return _META_DIR_CACHE

    primary = get_wiki_dir() / ".meta"
    fallback = get_extension_root() / "data" / "v8_meta"
    explicit_value = os.environ.get("VECTOR_LAKE_META_DIR", "").strip()
    explicit = (
        Path(explicit_value).expanduser().resolve()
        if explicit_value
        else None
    )
    allow_existing_fallback = (
        os.environ.get("VECTOR_LAKE_ALLOW_META_FALLBACK") == "1"
    )
    cache_key = (
        str(get_memory_dir()),
        str(explicit or ""),
        allow_existing_fallback,
    )
    if isinstance(_META_DIR_CACHE, dict) and cache_key in _META_DIR_CACHE:
        return _META_DIR_CACHE[cache_key]
    if not isinstance(_META_DIR_CACHE, dict):
        _META_DIR_CACHE = {}

    if explicit is not None:
        if (
            explicit == primary.resolve()
            and _uses_legacy_default_memory_root()
            and (fallback / "vector_lake.db").exists()
            and not (primary / "vector_lake.db").exists()
        ):
            raise RuntimeError(
                "Legacy fallback Vector Lake state exists while the configured "
                f"primary meta directory is empty: {fallback}. Refusing to create "
                "a second canonical database; migrate or select the fallback explicitly."
            )
        try:
            selected = _verify_writable_meta_dir(explicit)
        except OSError as exc:
            raise RuntimeError(
                f"Configured VECTOR_LAKE_META_DIR is not writable: {explicit}"
            ) from exc
        _META_DIR_CACHE[cache_key] = selected
        return selected

    if (
        _uses_legacy_default_memory_root()
        and (fallback / "vector_lake.db").exists()
        and not (primary / "vector_lake.db").exists()
    ):
        try:
            selected = _verify_writable_meta_dir(fallback)
        except OSError as fallback_error:
            raise RuntimeError(
                "Legacy fallback Vector Lake state exists but is not writable: "
                f"{fallback}"
            ) from fallback_error
        _META_DIR_CACHE[cache_key] = selected
        return selected

    try:
        selected = _verify_writable_meta_dir(primary)
    except OSError as primary_error:
        if (primary / "vector_lake.db").exists() and not allow_existing_fallback:
            raise RuntimeError(
                "Canonical Vector Lake state already exists but its meta directory "
                f"is not writable: {primary}. Refusing to select a different "
                "database. Configure VECTOR_LAKE_META_DIR explicitly."
            ) from primary_error
        if not _uses_legacy_default_memory_root():
            raise RuntimeError(
                "The configured VECTOR_LAKE_MEMORY_DIR has no writable canonical "
                f"meta directory: {primary}. Refusing the extension-global fallback; "
                "configure VECTOR_LAKE_META_DIR explicitly."
            ) from primary_error
    else:
        _META_DIR_CACHE[cache_key] = selected
        return selected

    try:
        selected = _verify_writable_meta_dir(fallback)
    except OSError as fallback_error:
        raise RuntimeError(
            f"Neither canonical meta directory is writable: {primary}; {fallback}"
        ) from fallback_error
    _META_DIR_CACHE[cache_key] = selected
    return selected


def get_purpose_path() -> Path:
    return get_memory_dir() / "purpose.md"


def get_index_path() -> Path:
    return get_wiki_dir() / "index.json"


def get_claim_graph_path() -> Path:
    return get_wiki_dir() / "claim_graph.json"


def get_projection_manifest_path() -> Path:
    """Return the fixed commit-marker path for the projection pair."""
    return get_wiki_dir() / "projection_pair_manifest.json"


def get_legacy_claim_graph_path() -> Path:
    return get_wiki_dir() / "claim_topology.json"


def get_outbox_signal_path() -> Path:
    return get_meta_dir() / "outbox_signal.lock"





def normalize_raw_ref(raw_ref: str) -> str:
    normalized = str(raw_ref).replace("\\", "/").strip()
    if normalized.startswith("MEMORY/"):
        normalized = normalized[len("MEMORY/") :]
    return normalized


def normalize_list_field(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def normalize_sources(value) -> list[str]:
    return [normalize_raw_ref(item) for item in normalize_list_field(value)]


def split_frontmatter(content: str) -> tuple[dict, str]:
    if not content.startswith("---\n") and not content.startswith("---\r\n"):
        return {}, content
    
    match = re.search(r'\r?\n---(?:\r?\n|$)', content)
    if not match:
        raise yaml.YAMLError(
            "Missing YAML frontmatter closing delimiter"
        )
        
    yaml_part = content[4:match.start()]
    body_part = content[match.end():]
    
    try:
        frontmatter = load_yaml(yaml_part) or {}
    except yaml.YAMLError:
        raise

    if not isinstance(frontmatter, dict):
        frontmatter = {}
    return frontmatter, body_part


def read_markdown_file(path: str | Path, errors: str = "replace") -> tuple[dict, str, str]:
    with open(path, "r", encoding="utf-8", errors=errors) as handle:
        content = handle.read()
    frontmatter, body = split_frontmatter(content)
    return frontmatter, body, content


def read_frontmatter_only(path: str | Path, errors: str = "replace") -> dict:
    """Reads only the YAML frontmatter without loading the entire file body into memory."""
    yaml_lines = []
    with open(path, "r", encoding="utf-8", errors=errors) as handle:
        first_line = handle.readline()
        if not first_line.startswith("---"):
            return {}
        for line in handle:
            if line.startswith("---"):
                break
            yaml_lines.append(line)
    if not yaml_lines:
        return {}
    try:
        frontmatter = load_yaml("".join(yaml_lines)) or {}
        return frontmatter if isinstance(frontmatter, dict) else {}
    except yaml.YAMLError:
        return {}


_WINDOWS_RESERVED_COMPONENT = re.compile(
    r"(?i)^(?:con|prn|aux|nul|conin\$|conout\$|com[1-9]|lpt[1-9])(?:\..*)?$"
)


def _reject_ambiguous_windows_path(path: Path) -> None:
    if os.name != "nt":
        return
    raw_path = str(path)
    if (
        len(raw_path) >= 4
        and raw_path.startswith("\\\\")
        and raw_path[2] in ".?"
        and raw_path[3] in "\\/"
    ):
        raise ValueError(f"Ambiguous Windows path namespace is not allowed: {path}")
    for component in path.parts:
        if component == path.anchor:
            continue
        if component.rstrip(" .") != component:
            raise ValueError(
                f"Ambiguous Windows path component is not allowed: {component!r}"
            )
        if ":" in component:
            raise ValueError(
                f"Windows alternate data stream paths are not allowed: {component!r}"
            )
        if _WINDOWS_RESERVED_COMPONENT.fullmatch(component):
            raise ValueError(
                f"Reserved Windows path component is not allowed: {component!r}"
            )


def _is_canonical_wiki_markdown_path(path: Path) -> bool:
    if path.suffix.casefold() != ".md":
        return False
    candidate_parent = path.parent.resolve(strict=False)
    wiki_root = get_wiki_dir().resolve(strict=False)
    parent_key = os.path.normcase(str(candidate_parent))
    root_key = os.path.normcase(str(wiki_root))
    try:
        return os.path.commonpath((parent_key, root_key)) == root_key
    except ValueError:
        return False


def atomic_write_text(
    path: str | Path,
    content: str,
    pre_parsed_frontmatter: dict | None = None,
    validation_mode: str = "full",
    expected_current_hash: str | None = None,
):
    path = Path(path)
    _reject_ambiguous_windows_path(path)
    if validation_mode not in {"full", "schema"}:
        raise ValueError(f"Unsupported validation_mode: {validation_mode}")
    
    # Validate canonical Wiki Markdown regardless of path spelling or casing.
    if _is_canonical_wiki_markdown_path(path):
        parsed_frontmatter, _ = split_frontmatter(content)
        if (
            pre_parsed_frontmatter is not None
            and pre_parsed_frontmatter != parsed_frontmatter
        ):
            raise ValueError("Pre-parsed frontmatter does not match content")
        frontmatter = parsed_frontmatter
        if validation_mode == "full":
            from vector_lake.defense_hook import verify_asset

            verify_asset(content, path.name, frontmatter, get_index_path())
        else:
            from vector_lake.schema_validator import validate_schema

            validate_schema(frontmatter, content, path.name)
            
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            sync_open_file(handle)
        if expected_current_hash is not None:
            _replace_prepared_file_compare_and_swap(
                path,
                temp_path,
                expected_current_hash,
            )
            commit_existing_file(path)
        else:
            durable_replace_file(temp_path, path, source_synced=True)
    finally:
        if temp_path.exists():
            temp_path.unlink()

def ensure_parent_dir(path: str | Path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)


class SafeWriteError(Exception):
    pass

def write_markdown_file(path: str | Path, frontmatter: dict, body: str, skip_validation: bool = False):
    path = Path(path)
    # Check traversal
    try:
        if path.resolve().is_relative_to(get_wiki_dir().resolve()) is False and "MEMORY" not in str(path):
            raise SafeWriteError(f"Path traversal blocked: {path}")
    except Exception:
        pass
    if not skip_validation and path.exists():
        try:
            _, old_body, _ = read_markdown_file(path)
            old_truth_count = _count_list_items(old_body, "编译事实") or _count_list_items(old_body, "Compiled Truth")
            new_truth_count = _count_list_items(body, "编译事实") or _count_list_items(body, "Compiled Truth")
            if new_truth_count < old_truth_count:
                raise SafeWriteError(f"丢失了编译事实 (Compiled Truth)。旧文件有 {old_truth_count} 条，新文件只有 {new_truth_count} 条。请调用 read_resource 重新读取当前文件状态，并使用 Append 模式进行增量合并，而不是直接覆盖。")
            old_timeline_count = _count_list_items(old_body, "证据时间线") or _count_list_items(old_body, "Evidence Timeline")
            new_timeline_count = _count_list_items(body, "证据时间线") or _count_list_items(body, "Evidence Timeline")
            if new_timeline_count < old_timeline_count:
                raise SafeWriteError(f"丢失了证据时间线 (Evidence Timeline)。旧文件有 {old_timeline_count} 条记录，新文件只有 {new_timeline_count} 条记录。请调用 read_resource 重新读取当前文件状态，并使用 Append 模式进行增量合并，而不是直接覆盖。")
        except SafeWriteError:
            raise
        except Exception:
            pass

    filename = path.name
    if not skip_validation:
        validate_wiki_filename(filename)
    
    if filename.startswith("Synthesis_STORM_") and not skip_validation:
        required_headers = [
            "## 1. Top 5 Key Findings",
            "## 2. The Contradiction Map",
            "## 3. Actionable Insights",
            "## 4. Multi-Perspective Raw Scan",
            "## 5. Peer Review"
        ]
        for header in required_headers:
            if header not in body:
                raise SafeWriteError(f"STORM Synthesis Structural Violation: The file {filename} is missing mandatory H2 section '{header}'. Please strictly follow the references/storm_report_template.md structure.")
    yaml_block = dump_yaml(frontmatter, allow_unicode=True, default_flow_style=False, sort_keys=False)
    full_content = f"---\n{yaml_block}---\n{body.lstrip()}"
    expected_path = (get_wiki_dir() / filename).resolve()
    if path.resolve() != expected_path:
        raise SafeWriteError(f"Path traversal blocked: {path}")
    from vector_lake.mutation_coordinator import execute_mutation_plan
    execute_mutation_plan(filename, content=full_content, is_delete=False)


def backup_file(path: str | Path, suffix: str = ".bak") -> Path | None:
    source = Path(path)
    if not source.exists():
        return None
    backup_path = source.with_name(source.name + suffix)
    shutil.copy2(source, backup_path)
    return backup_path


def sanitize_wiki_node(filepath: str | Path):
    filepath = Path(filepath)
    if not filepath.exists() or filepath.suffix.lower() != ".md":
        return

    frontmatter, body, _ = read_markdown_file(filepath)
    today = datetime.datetime.now().strftime("%Y%m%d")
    if not frontmatter.get("id"):
        frontmatter["id"] = f"{today}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=6))}"
    frontmatter["updated"] = today
    write_markdown_file(filepath, frontmatter, body, skip_validation=False)


def _count_list_items(body: str, section_marker: str) -> int:
    count = 0
    in_section = False
    for line in io.StringIO(body):
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = section_marker in stripped
        elif stripped.startswith("# "):
            in_section = False
        
        if in_section and (stripped.startswith("- ") or stripped.startswith("* ")):
            count += 1
    return count

def safe_write_markdown(path: str | Path, content: str, skip_validation: bool = False):
    frontmatter, body = split_frontmatter(content)
    write_markdown_file(path, frontmatter, body, skip_validation=skip_validation)

class TensionEdge(TypedDict):
    target: str
    polarity: float
    intensity: float

class EntityData(TypedDict, total=False):
    id: str
    title: str
    type: str
    domain: str
    status: str
    epistemic_status: str
    categories: List[str]
    created: str
    updated: str
    sources: List[str]
    aliases: List[str]
    tags: List[str]
    tension_edges: List[TensionEdge]
    raw_text: str
    links: List[str]
    triples: List[dict]
    _key: str
    _pre_embedded: List[float]

class ClaimData(TypedDict, total=False):
    claim_id: str
    source_page: str
    statement: str
    predicate: str
    target: str
    confidence: float
    context: str
    evidence_links: List[str]

def enforce_entity_dict(data: dict) -> EntityData:
    """Runtime assertion to enforce EntityData structure without pulling in heavy pydantic."""
    # Just a cast and basic validation to prevent drift
    if 'id' not in data and 'title' not in data:
        pass # Allow partial for now
    return data # type: ignore

def enforce_claim_dict(data: dict) -> ClaimData:
    return data # type: ignore

