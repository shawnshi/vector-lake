import datetime
import os
import random
import re
import shutil
import string
import uuid
from pathlib import Path

import yaml
from vector_lake.yaml_utils import load_yaml, dump_yaml
from vector_lake import get_extension_root


FRONTMATTER_REGEX = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_META_DIR_CACHE = None

SYSTEM_WHITELIST = {"index.md", "log.md", "overview.md", "orphan_pages.md", "wiki_link_stats.md", "Synthesis_log.md"}
VALID_PREFIXES = ("Concept_", "Vendor_", "Product_", "Person_", "Event_", "Source_", "Synthesis_")
INVALID_CHARS_REGEX = re.compile(r'[\[\]<>:"/\\|\?\*\(\)\s]+')

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
    if filename in SYSTEM_WHITELIST:
        return
    
    if not filename.endswith(".md"):
        raise ValueError(f"Invalid suffix: '{filename}' must end with .md")
        
    if not filename.startswith(VALID_PREFIXES):
        raise ValueError(f"Invalid prefix: '{filename}' must start with one of {VALID_PREFIXES}")
        
    if INVALID_CHARS_REGEX.search(filename):
        raise ValueError(f"Invalid characters: '{filename}' contains forbidden characters (e.g., brackets, slashes, spaces).")
        
    if not re.match(r'^(Concept|Vendor|Product|Person|Event|Source|Synthesis)_[a-zA-Z0-9\u4e00-\u9fa5]+(-[a-zA-Z0-9\u4e00-\u9fa5]+)*\.md$', filename):
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
    return (Path(os.path.expanduser("~")) / ".gemini" / "MEMORY").resolve()


def get_wiki_dir() -> Path:
    return get_memory_dir() / "wiki"


def get_raw_dir() -> Path:
    return get_memory_dir() / "raw"


def get_meta_dir() -> Path:
    global _META_DIR_CACHE
    if _META_DIR_CACHE is not None:
        return _META_DIR_CACHE

    primary = get_wiki_dir() / ".meta"
    fallback = get_extension_root() / "data" / "v8_meta"

    for candidate in (primary, fallback):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / f".probe_{uuid.uuid4().hex}"
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("probe")
            probe.unlink()
            _META_DIR_CACHE = candidate
            return candidate
        except OSError:
            continue

    _META_DIR_CACHE = fallback
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def get_purpose_path() -> Path:
    return get_memory_dir() / "purpose.md"


def get_index_path() -> Path:
    return get_wiki_dir() / "index.json"


def get_claim_graph_path() -> Path:
    return get_wiki_dir() / "claim_graph.json"





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
    match = FRONTMATTER_REGEX.match(content)
    if not match:
        return {}, content

    try:
        frontmatter = load_yaml(match.group(1)) or {}
    except yaml.YAMLError:
        raise

    if not isinstance(frontmatter, dict):
        frontmatter = {}
    return frontmatter, match.group(2)


def read_markdown_file(path: str | Path, errors: str = "replace") -> tuple[dict, str, str]:
    with open(path, "r", encoding="utf-8", errors=errors) as handle:
        content = handle.read()
    frontmatter, body = split_frontmatter(content)
    return frontmatter, body, content


def atomic_write_text(path: str | Path, content: str):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(temp_path, path)
import datetime
import os
import random
import re
import shutil
import string
import uuid
from pathlib import Path

import yaml
from vector_lake.yaml_utils import load_yaml, dump_yaml
from vector_lake import get_extension_root


FRONTMATTER_REGEX = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_META_DIR_CACHE = None

SYSTEM_WHITELIST = {"index.md", "log.md", "overview.md", "orphan_pages.md", "wiki_link_stats.md", "Synthesis_log.md"}
VALID_PREFIXES = ("Concept_", "Vendor_", "Product_", "Person_", "Event_", "Source_", "Synthesis_")
INVALID_CHARS_REGEX = re.compile(r'[\[\]<>:"/\\|\?\*\(\)\s]+')

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
    if filename in SYSTEM_WHITELIST:
        return
    
    if not filename.endswith(".md"):
        raise ValueError(f"Invalid suffix: '{filename}' must end with .md")
        
    if not filename.startswith(VALID_PREFIXES):
        raise ValueError(f"Invalid prefix: '{filename}' must start with one of {VALID_PREFIXES}")
        
    if INVALID_CHARS_REGEX.search(filename):
        raise ValueError(f"Invalid characters: '{filename}' contains forbidden characters (e.g., brackets, slashes, spaces).")
        
    if not re.match(r'^(Concept|Vendor|Product|Person|Event|Source|Synthesis)_[a-zA-Z0-9\u4e00-\u9fa5]+(-[a-zA-Z0-9\u4e00-\u9fa5]+)*\.md$', filename):
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
    return (Path(os.path.expanduser("~")) / ".gemini" / "MEMORY").resolve()


def get_wiki_dir() -> Path:
    return get_memory_dir() / "wiki"


def get_raw_dir() -> Path:
    return get_memory_dir() / "raw"


def get_meta_dir() -> Path:
    global _META_DIR_CACHE
    if _META_DIR_CACHE is not None:
        return _META_DIR_CACHE

    primary = get_wiki_dir() / ".meta"
    fallback = get_extension_root() / "data" / "v8_meta"

    for candidate in (primary, fallback):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / f".probe_{uuid.uuid4().hex}"
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("probe")
            probe.unlink()
            _META_DIR_CACHE = candidate
            return candidate
        except OSError:
            continue

    _META_DIR_CACHE = fallback
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def get_purpose_path() -> Path:
    return get_memory_dir() / "purpose.md"


def get_index_path() -> Path:
    return get_wiki_dir() / "index.json"


def get_claim_graph_path() -> Path:
    return get_wiki_dir() / "claim_graph.json"





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
    match = FRONTMATTER_REGEX.match(content)
    if not match:
        return {}, content

    try:
        frontmatter = load_yaml(match.group(1)) or {}
    except yaml.YAMLError:
        raise

    if not isinstance(frontmatter, dict):
        frontmatter = {}
    return frontmatter, match.group(2)


def read_markdown_file(path: str | Path, errors: str = "replace") -> tuple[dict, str, str]:
    with open(path, "r", encoding="utf-8", errors=errors) as handle:
        content = handle.read()
    frontmatter, body = split_frontmatter(content)
    return frontmatter, body, content


def atomic_write_text(path: str | Path, content: str):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    with open(temp_path, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(temp_path, path)


def ensure_parent_dir(path: str | Path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def write_markdown_file(path: str | Path, frontmatter: dict, body: str, skip_validation: bool = False):
    path = Path(path)
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
    backup_file(path)
    atomic_write_text(path, f"---\n{yaml_block}---\n{body.lstrip()}")


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


class SafeWriteError(Exception):
    pass

def _count_list_items(body: str, section_marker: str) -> int:
    count = 0
    in_section = False
    for line in body.splitlines():
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
