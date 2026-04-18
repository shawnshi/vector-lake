import logging
import os

import yaml

from vector_lake import indexer
from vector_lake.wiki_utils import get_memory_dir, get_wiki_dir, normalize_sources, read_markdown_file, write_markdown_file


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-delete")


def delete_source(raw_path: str, dry_run: bool = False) -> str:
    wiki_dir = str(get_wiki_dir())
    memory_dir = str(get_memory_dir())

    raw_basename = os.path.basename(raw_path)
    raw_stem = os.path.splitext(raw_basename)[0]
    try:
        raw_ref = os.path.relpath(raw_path, memory_dir).replace("\\", "/")
    except ValueError:
        raw_ref = raw_path.replace("\\", "/")

    raw_ref_lower = raw_ref.lower()
    raw_basename_lower = raw_basename.lower()

    if not os.path.exists(wiki_dir):
        return "Wiki directory not found."

    actions = []
    for filename in os.listdir(wiki_dir):
        if not filename.endswith(".md") or filename in ("index.md", "log.md", "overview.md"):
            continue

        filepath = os.path.join(wiki_dir, filename)
        try:
            frontmatter, body, _ = read_markdown_file(filepath)
        except Exception:
            continue

        sources = normalize_sources(frontmatter.get("sources", []))
        is_source_page = filename.lower().startswith(f"source_{raw_stem.lower()}")
        has_source_ref = any(raw_ref_lower in source.lower() or raw_basename_lower in source.lower() for source in sources)
        if not (is_source_page or has_source_ref):
            continue

        if len(sources) <= 1 or is_source_page:
            actions.append(("DELETE", filepath, filename, None, None))
        else:
            new_sources = [source for source in sources if raw_ref_lower not in source.lower() and raw_basename_lower not in source.lower()]
            frontmatter["sources"] = new_sources
            actions.append(("REMOVE_REF", filepath, f"{filename}: {len(sources)}→{len(new_sources)} sources", frontmatter, body))

    lines = [f"[CASCADE DELETE] Requested raw source: {raw_path}"]
    if os.path.exists(raw_path):
        lines.append(f"  [DELETE_RAW] {raw_path}")
    else:
        lines.append(f"  [MISSING_RAW] {raw_path}")

    if actions:
        lines.append(f"  [WIKI] {len(actions)} affected wiki page(s):")
        for action, _, detail, _, _ in actions:
            lines.append(f"    [{action}] {detail}")
    else:
        lines.append("  [WIKI] No related wiki pages found.")

    if dry_run:
        lines.append("")
        lines.append("(Dry run — no changes made. Remove --dry-run to execute.)")
        return "\n".join(lines)

    deleted = 0
    updated = 0
    failures = []
    for action, filepath, _, frontmatter, body in actions:
        if action == "DELETE":
            try:
                os.remove(filepath)
                deleted += 1
                log.info(f"Deleted: {filepath}")
            except Exception as e:
                failures.append(f"DELETE {filepath}: {e}")
                log.warning(f"Failed to delete {filepath}: {e}")
        elif action == "REMOVE_REF":
            try:
                write_markdown_file(filepath, frontmatter, body)
                updated += 1
                log.info(f"Removed source ref from: {filepath}")
            except Exception as e:
                failures.append(f"REMOVE_REF {filepath}: {e}")
                log.warning(f"Failed to update {filepath}: {e}")

    raw_deleted = False
    if failures:
        log.warning("Skipping raw source deletion because wiki cleanup had failures.")
    elif os.path.exists(raw_path):
        try:
            os.remove(raw_path)
            raw_deleted = True
            log.info(f"Deleted raw source: {raw_path}")
        except Exception as e:
            log.warning(f"Failed to delete raw source {raw_path}: {e}")

    indexer.generate_index()
    lines.append("")
    lines.append(f"Executed: raw_deleted={raw_deleted}, wiki_deleted={deleted}, wiki_updated={updated}. Index rebuilt.")
    if failures:
        lines.append("Warnings:")
        for failure in failures:
            lines.append(f"  {failure}")
        lines.append("Raw source was preserved because wiki cleanup did not complete successfully.")
    return "\n".join(lines)

