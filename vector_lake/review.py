"""
Vector Lake Review Queue — Async Human-in-the-Loop System.

Parses REVIEW blocks from Ingestor Agent output, stores them in a JSON queue,
and provides CLI interface for users to inspect and resolve review items.
"""
import os
import json
import re
import logging
from datetime import datetime, timezone
from pathlib import Path
from filelock import FileLock

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-review")

WIKI_DIR = Path(os.path.expanduser("~")) / ".gemini" / "MEMORY" / "wiki"
REVIEW_FILE = WIKI_DIR / ".meta" / "review_queue.json"

# Valid review types (constrained to prevent LLM hallucination)
VALID_TYPES = {"contradiction", "duplicate", "missing-page", "suggestion"}

REVIEW_BLOCK_REGEX = re.compile(
    r'---REVIEW:\s*(\w[\w-]*)\s*\|\s*(.+?)\s*---\n([\s\S]*?)---END REVIEW---'
)


def _ensure_meta_dir():
    """Ensure .meta directory exists."""
    meta_dir = WIKI_DIR / ".meta"
    meta_dir.mkdir(parents=True, exist_ok=True)


def _queue_lock() -> FileLock:
    _ensure_meta_dir()
    return FileLock(str(REVIEW_FILE) + ".lock", timeout=10)


def _load_queue_unlocked() -> list:
    """Load review queue from disk without taking a lock."""
    if not REVIEW_FILE.exists():
        return []
    try:
        with open(REVIEW_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_queue_unlocked(items: list):
    """Atomically save review queue to disk without taking a lock."""
    _ensure_meta_dir()
    temp_path = str(REVIEW_FILE) + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, str(REVIEW_FILE))


def _load_queue() -> list:
    with _queue_lock():
        return _load_queue_unlocked()


def _save_queue(items: list):
    with _queue_lock():
        _save_queue_unlocked(items)


def parse_review_blocks(agent_output: str, source_path: str) -> list:
    """Parse ---REVIEW: type | title--- blocks from Ingestor Agent output.
    
    Returns list of review item dicts (not yet persisted).
    """
    items = []
    for match in REVIEW_BLOCK_REGEX.finditer(agent_output):
        raw_type = match.group(1).strip().lower()
        title = match.group(2).strip()
        body = match.group(3).strip()

        # Constrain type to valid set
        review_type = raw_type if raw_type in VALID_TYPES else "suggestion"

        # Parse SEARCH line (for Deep Research)
        search_match = re.search(r'^SEARCH:\s*(.+)$', body, re.MULTILINE)
        search_queries = []
        if search_match:
            search_queries = [q.strip() for q in search_match.group(1).split("|") if q.strip()]

        # Parse PAGES line
        pages_match = re.search(r'^PAGES:\s*(.+)$', body, re.MULTILINE)
        affected_pages = []
        if pages_match:
            affected_pages = [p.strip() for p in pages_match.group(1).split(",") if p.strip()]

        # Description is body minus SEARCH and PAGES lines
        description = body
        description = re.sub(r'^SEARCH:.*$', '', description, flags=re.MULTILINE)
        description = re.sub(r'^PAGES:.*$', '', description, flags=re.MULTILINE)
        description = description.strip()

        items.append({
            "type": review_type,
            "title": title,
            "description": description,
            "source": source_path,
            "search_queries": search_queries,
            "affected_pages": affected_pages,
            "created": datetime.now(timezone.utc).isoformat(),
            "resolved": False,
            "resolution": None,
        })

    return items


def add_items(items: list):
    """Add new review items to the persistent queue."""
    if not items:
        return
    with _queue_lock():
        queue = _load_queue_unlocked()
        queue.extend(items)
        _save_queue_unlocked(queue)
    log.info(f"Added {len(items)} review item(s) to queue. Total: {len(queue)} pending.")


def get_pending() -> list:
    """Return all unresolved review items."""
    with _queue_lock():
        queue = _load_queue_unlocked()
        return [item for item in queue if not item.get("resolved", False)]


def resolve_item(index: int, resolution: str = "skip"):
    """Mark a review item as resolved by its index in the pending list."""
    with _queue_lock():
        queue = _load_queue_unlocked()
        pending_indices = [i for i, item in enumerate(queue) if not item.get("resolved", False)]

        if index < 0 or index >= len(pending_indices):
            log.error(f"Invalid review index: {index}. Pending items: {len(pending_indices)}")
            return None

        real_index = pending_indices[index]
        queue[real_index]["resolved"] = True
        queue[real_index]["resolution"] = resolution
        queue[real_index]["resolved_at"] = datetime.now(timezone.utc).isoformat()
        _save_queue_unlocked(queue)
        log.info(f"Resolved review item #{index}: '{queue[real_index]['title']}' → {resolution}")
        return queue[real_index]


def format_pending_report() -> str:
    """Format pending review items as a CLI-readable report."""
    pending = get_pending()
    if not pending:
        return "[OK] No pending review items."
    
    lines = [f"[REVIEW] {len(pending)} Pending Review Items\n"]
    
    TYPE_ICONS = {
        "contradiction": "[!]",
        "duplicate": "[D]",
        "missing-page": "[?]",
        "suggestion": "[*]",
    }
    
    for i, item in enumerate(pending):
        icon = TYPE_ICONS.get(item["type"], "❓")
        lines.append(f"  [{i}] {icon} **{item['title']}** ({item['type']})")
        lines.append(f"      Source: {item.get('source', 'unknown')}")
        if item.get("description"):
            desc = item["description"][:120]
            lines.append(f"      {desc}")
        if item.get("search_queries"):
            lines.append(f"      Research queries: {' | '.join(item['search_queries'])}")
        if item.get("affected_pages"):
            lines.append(f"      Pages: {', '.join(item['affected_pages'])}")
        lines.append("")
    
    lines.append("Actions: `python cli.py review resolve <index> [skip|create]`")
    return "\n".join(lines)

