import logging
import os
import shutil
import json
from pathlib import Path

import yaml

from vector_lake import indexer
from vector_lake.wiki_utils import get_memory_dir, get_wiki_dir, normalize_sources, read_markdown_file, write_markdown_file
from vector_lake.db_store import delete_node_cascade, get_connection


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-delete")


def delete_source(raw_path: str, dry_run: bool = True) -> str:
    wiki_dir = Path(get_wiki_dir()).resolve(strict=True)
    memory_dir = Path(get_memory_dir()).resolve(strict=True)
    raw_memory_dir = memory_dir / "raw"
    
    raw_path_obj = Path(raw_path).resolve()
    if not raw_path_obj.is_relative_to(raw_memory_dir):
        return f"[Security Error] The target '{raw_path}' is not within {raw_memory_dir}. Only raw sources can be deleted this way."

    raw_basename = raw_path_obj.name
    raw_stem = raw_path_obj.stem
    try:
        raw_ref = str(raw_path_obj.relative_to(memory_dir)).replace("\\", "/")
    except ValueError:
        raw_ref = str(raw_path_obj).replace("\\", "/")

    raw_ref_lower = raw_ref.lower()
    raw_basename_lower = raw_basename.lower()

    if not wiki_dir.exists():
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
        lines.append("(Dry run — no changes made. Re-run with dry_run=False to execute.)")
        return "\n".join(lines)

    deleted = 0
    updated = 0
    failures = []
    
    from vector_lake.db_store import transaction
    from vector_lake.wiki_utils import atomic_write_text
    from vector_lake.governance_store import sync_pages_to_canonical
    from vector_lake.indexer import update_index_items, delete_index_items
    import shutil
    
    backups = {}
    tmp_dir = get_extension_root() / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    
    deleted_files = []
    updated_files = []
    
    try:
        with transaction():
            for action, filepath, _, frontmatter, body in actions:
                filename = os.path.basename(filepath)
                if action == "DELETE":
                    try:
                        bak_path = tmp_dir / f"{filename}.bak"
                        shutil.copy2(filepath, bak_path)
                        backups[filepath] = bak_path
                        os.remove(filepath)
                        deleted_files.append(filename)
                        deleted += 1
                        log.info(f"Deleted (backed up): {filepath}")
                    except Exception as e:
                        failures.append(f"DELETE {filepath}: {e}")
                        log.warning(f"Failed to delete {filepath}: {e}")
                elif action == "REMOVE_REF":
                    try:
                        bak_path = tmp_dir / f"{filename}.bak"
                        shutil.copy2(filepath, bak_path)
                        backups[filepath] = bak_path
                        
                        fm_str = yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)
                        new_content = f"---\n{fm_str}---\n{body}"
                        atomic_write_text(filepath, new_content)
                        updated_files.append(filename)
                        updated += 1
                        log.info(f"Removed source ref from: {filepath}")
                    except Exception as e:
                        failures.append(f"REMOVE_REF {filepath}: {e}")
                        log.warning(f"Failed to update {filepath}: {e}")

            if failures:
                raise Exception("Failures occurred during file operations, rolling back.")

            # Batch sync
            if deleted_files or updated_files:
                sync_pages_to_canonical(
                    [os.path.join(wiki_dir, f) for f in updated_files],
                    origin="tool_delete",
                    auto_approve=True,
                    summary=f"Cascade delete from {raw_path}",
                    deleted_files=deleted_files
                )
                if updated_files:
                    update_index_items(updated_files)
                if deleted_files:
                    delete_index_items(deleted_files)

    except Exception as e:
        for orig, bak in backups.items():
            if bak.exists():
                if os.path.exists(orig):
                    os.remove(orig)
                shutil.move(str(bak), str(orig))
        failures.append(str(e))
        log.error(f"Transaction failed, rolled back: {e}")
    finally:
        for bak in backups.values():
            if bak.exists():
                os.remove(bak)

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

    from vector_lake import get_extension_root
    tmp_dir = get_extension_root() / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with open(tmp_dir / "flag_reindex.lock", "w") as f:
        f.write("1")
        
    lines.append("")
    lines.append(f"Executed: raw_deleted={raw_deleted}, wiki_deleted={deleted}, wiki_updated={updated}. Async index rebuild scheduled.")
    if failures:
        lines.append("Warnings:")
        for failure in failures:
            lines.append(f"  {failure}")
        lines.append("Raw source was preserved because wiki cleanup did not complete successfully.")
    return "\n".join(lines)

