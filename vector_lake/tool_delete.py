import hashlib
import logging
import os
import re
import stat
import uuid
from pathlib import Path

import yaml

from vector_lake.wiki_utils import (
    SYSTEM_WHITELIST,
    get_memory_dir,
    get_wiki_dir,
    normalize_semantic_text,
    normalize_raw_ref,
    normalize_sources,
    split_frontmatter,
)


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("vector-lake-tool-delete")


def _source_ref_identity(value: object) -> str:
    normalized = normalize_raw_ref(str(value or ""))
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.casefold()


def _frontmatter_integrity_error(content: str, frontmatter: dict) -> str | None:
    if not content.startswith(("---\n", "---\r\n")):
        return "missing YAML frontmatter opening delimiter"
    if re.search(r"\r?\n---(?:\r?\n|$)", content) is None:
        return "missing YAML frontmatter closing delimiter"
    if not frontmatter:
        return "empty or non-mapping YAML frontmatter"
    return None


def _compact_exception(exc: Exception) -> str:
    detail = " ".join(str(exc).split())
    if not detail:
        detail = exc.__class__.__name__
    return detail


_RAW_HASH_CHUNK_BYTES = 1024 * 1024
_RAW_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _raw_stat_key(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _raw_object_key(value: os.stat_result) -> tuple[int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size)


def _capture_raw_revision(raw_path: Path) -> tuple | None:
    """Return a stable raw-file revision fingerprint or None when absent."""
    for _attempt in range(3):
        try:
            handle = raw_path.open("rb")
        except FileNotFoundError:
            return None

        with handle:
            stat_before = os.fstat(handle.fileno())
            if not stat.S_ISREG(stat_before.st_mode):
                raise RuntimeError(f"Raw source is not a regular file: {raw_path}")
            if getattr(stat_before, "st_file_attributes", 0) & _RAW_REPARSE_POINT:
                raise RuntimeError(f"Raw source is a reparse point: {raw_path}")
            digest = hashlib.sha256()
            while chunk := handle.read(_RAW_HASH_CHUNK_BYTES):
                digest.update(chunk)
            stat_after = os.fstat(handle.fileno())

        try:
            path_stat = os.stat(raw_path, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if _raw_stat_key(stat_before) == _raw_stat_key(stat_after) and _raw_object_key(
            stat_after
        ) == _raw_object_key(path_stat):
            return (*_raw_stat_key(path_stat), digest.hexdigest())

    raise RuntimeError(f"Raw source changed while hashing: {raw_path}")


def _restore_quarantined_raw(quarantine_path: Path, raw_path: Path) -> Path:
    """Best-effort restore while retaining the quarantined revision."""
    try:
        os.link(quarantine_path, raw_path, follow_symlinks=False)
    except OSError:
        pass

    # Never unlink the quarantine copy on a recovery path. A concurrent writer
    # can replace raw_path immediately after os.link() (or after any subsequent
    # identity check), so removing this last known link could destroy the
    # revision that recovery is meant to preserve.
    log.warning("Retained quarantined raw revision at %s", quarantine_path)
    return quarantine_path


def delete_source(raw_path: str, dry_run: bool = True) -> str:
    wiki_dir = Path(get_wiki_dir()).resolve()
    memory_dir = Path(get_memory_dir()).resolve()
    raw_memory_dir = (memory_dir / "raw").resolve()

    raw_path_obj = Path(raw_path).resolve()
    if not raw_path_obj.is_relative_to(raw_memory_dir):
        return f"[Security Error] The target '{raw_path}' is not within {raw_memory_dir}. Only raw sources can be deleted this way."
    try:
        requested_raw_revision = _capture_raw_revision(raw_path_obj)
    except (OSError, RuntimeError) as exc:
        return (
            "[BLOCKED] Cannot capture a stable raw source revision: "
            f"{_compact_exception(exc)}\nNo changes made."
        )

    try:
        raw_ref = str(raw_path_obj.relative_to(memory_dir)).replace("\\", "/")
    except ValueError:
        raw_ref = str(raw_path_obj).replace("\\", "/")
    raw_ref_identity = _source_ref_identity(raw_ref)

    from vector_lake.tool_ingest import canonical_source_name

    expected_source_filename = canonical_source_name(
        str(raw_path_obj),
        source_identity_index={},
    ).casefold()

    if not wiki_dir.exists():
        return "Wiki directory not found."

    actions = []
    scan_failures = []
    filenames = sorted(
        entry.name
        for entry in wiki_dir.iterdir()
        if entry.is_file() and entry.suffix.casefold() == ".md"
    )
    skip_files = {name.casefold() for name in SYSTEM_WHITELIST}
    for filename in filenames:
        if filename.casefold() in skip_files:
            continue

        filepath = wiki_dir / filename
        try:
            page_bytes = filepath.read_bytes()
            content = normalize_semantic_text(page_bytes.decode("utf-8"))
            frontmatter, body = split_frontmatter(content)
            projection_hash = hashlib.sha256(page_bytes).hexdigest()
        except Exception as exc:
            scan_failures.append(
                f"{filename}: cannot parse YAML frontmatter or read strict UTF-8: "
                f"{_compact_exception(exc)}"
            )
            continue
        integrity_error = _frontmatter_integrity_error(content, frontmatter)
        if integrity_error:
            scan_failures.append(f"{filename}: {integrity_error}")
            continue

        sources = normalize_sources(frontmatter.get("sources", []))
        source_identities = [_source_ref_identity(source) for source in sources]
        has_source_ref = raw_ref_identity in source_identities
        is_canonical_source_page = filename.casefold() == expected_source_filename
        if not (is_canonical_source_page or has_source_ref):
            continue

        if len(sources) <= 1 or is_canonical_source_page:
            actions.append(("DELETE", filepath, filename, None, None, projection_hash))
        else:
            new_sources = [
                source
                for source, identity in zip(
                    sources,
                    source_identities,
                    strict=True,
                )
                if identity != raw_ref_identity
            ]
            frontmatter["sources"] = new_sources
            actions.append(
                (
                    "REMOVE_REF",
                    filepath,
                    f"{filename}: {len(sources)}→{len(new_sources)} sources",
                    frontmatter,
                    body,
                    projection_hash,
                )
            )

    lines = [f"[CASCADE DELETE] Requested raw source: {raw_path_obj}"]
    if os.path.exists(raw_path_obj):
        lines.append(f"  [DELETE_RAW] {raw_path_obj}")
    else:
        lines.append(f"  [MISSING_RAW] {raw_path_obj}")

    if actions:
        lines.append(f"  [WIKI] {len(actions)} affected wiki page(s):")
        for action, _, detail, _, _, _ in actions:
            lines.append(f"    [{action}] {detail}")
    else:
        lines.append("  [WIKI] No related wiki pages found.")

    if scan_failures:
        lines.append("")
        lines.append(
            f"[BLOCKED] {len(scan_failures)} Wiki Markdown file(s) have "
            "unreadable or invalid frontmatter:"
        )
        lines.extend(f"  - {failure}" for failure in scan_failures[:20])
        if len(scan_failures) > 20:
            lines.append(f"  ... and {len(scan_failures) - 20} more.")
        lines.append("No changes made; the raw source was preserved.")
        return "\n".join(lines)

    if dry_run:
        lines.append("")
        lines.append(
            "(Dry run — no changes made. Re-run with dry_run=False to execute.)"
        )
        return "\n".join(lines)

    deleted = 0
    updated = 0
    failures = []

    from vector_lake.mutation_coordinator import execute_mutation_batch

    mutations = []
    projection_preconditions = {}
    for (
        action,
        filepath,
        _,
        frontmatter,
        body,
        projection_hash,
    ) in actions:
        filename = filepath.name
        projection_preconditions[filepath] = projection_hash
        if action == "DELETE":
            mutations.append(
                {
                    "filename": filename,
                    "is_delete": True,
                    "expected_projection_hash": projection_hash,
                }
            )
            deleted += 1
        elif action == "REMOVE_REF":
            fm_str = yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)
            mutations.append(
                {
                    "filename": filename,
                    "content": f"---\n{fm_str}---\n{body}",
                    "expected_projection_hash": projection_hash,
                }
            )
            updated += 1

    def verify_projection_snapshot():
        try:
            current_raw_revision = _capture_raw_revision(raw_path_obj)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(
                f"Raw source changed after delete request: {_compact_exception(exc)}"
            ) from exc
        if current_raw_revision != requested_raw_revision:
            raise RuntimeError(
                f"Raw source changed after delete request: {raw_path_obj}"
            )
        for filepath, expected_hash in projection_preconditions.items():
            try:
                current_hash = hashlib.sha256(filepath.read_bytes()).hexdigest()
            except OSError as exc:
                raise RuntimeError(
                    f"Wiki projection changed after delete scan: {filepath.name}"
                ) from exc
            if current_hash != expected_hash:
                raise RuntimeError(
                    f"Wiki projection changed after delete scan: {filepath.name}"
                )

    if mutations:
        try:
            batch_result = execute_mutation_batch(
                mutations,
                precondition_callback=verify_projection_snapshot,
                return_details=True,
            )
            deferred = list(batch_result.get("deferred") or [])
            if deferred:
                failures.append("ATOMIC_WIKI_CLEANUP_DEFERRED: " + ", ".join(deferred))
                deleted = 0
                updated = 0
        except Exception as exc:
            failures.append(f"ATOMIC_WIKI_CLEANUP: {exc}")
            deleted = 0
            updated = 0
            log.warning("Atomic wiki cleanup failed: %s", exc)

    raw_deleted = False
    if failures:
        log.warning("Skipping raw source deletion because wiki cleanup had failures.")
    else:
        try:
            current_raw_revision = _capture_raw_revision(raw_path_obj)
            if current_raw_revision is None:
                pass
            elif current_raw_revision != requested_raw_revision:
                failures.append(
                    "RAW_SOURCE_CHANGED: source revision changed after "
                    "the delete request"
                )
                log.warning(
                    "Preserving changed raw source revision: %s",
                    raw_path_obj,
                )
            else:
                quarantine_path = raw_path_obj.with_name(
                    f".{raw_path_obj.name}.vector-lake-delete-"
                    f"{uuid.uuid4().hex}.quarantine"
                )
                os.replace(raw_path_obj, quarantine_path)
                try:
                    quarantine_revision = _capture_raw_revision(quarantine_path)
                    if quarantine_revision == requested_raw_revision:
                        os.remove(quarantine_path)
                        raw_deleted = True
                        log.info("Deleted raw source: %s", raw_path_obj)
                    else:
                        preserved_at = _restore_quarantined_raw(
                            quarantine_path,
                            raw_path_obj,
                        )
                        failures.append(
                            "RAW_SOURCE_QUARANTINE_MISMATCH: moved revision "
                            f"did not match request; preserved at {preserved_at}"
                        )
                        log.warning(
                            "Preserved swapped raw revision at %s",
                            preserved_at,
                        )
                except Exception as exc:
                    preserved_at = _restore_quarantined_raw(
                        quarantine_path,
                        raw_path_obj,
                    )
                    failures.append(
                        f"RAW_SOURCE_DELETE: {_compact_exception(exc)}; "
                        f"preserved_at={preserved_at}"
                    )
                    log.warning(
                        "Failed to delete raw source %s; preserved at %s: %s",
                        raw_path_obj,
                        preserved_at,
                        exc,
                    )
        except Exception as exc:
            failures.append(f"RAW_SOURCE_DELETE: {_compact_exception(exc)}")
            log.warning("Failed to delete raw source %s: %s", raw_path_obj, exc)

    lines.append("")
    lines.append(
        f"Executed: raw_deleted={raw_deleted}, wiki_deleted={deleted}, wiki_updated={updated}. Projection updates queued transactionally."
    )
    if failures:
        lines.append("Warnings:")
        for failure in failures:
            lines.append(f"  {failure}")
        lines.append(
            "Raw source was preserved because cleanup did not complete safely."
        )
    return "\n".join(lines)
