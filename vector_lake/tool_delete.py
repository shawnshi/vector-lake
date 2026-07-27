import logging
import os
import re
from pathlib import Path

import yaml

from vector_lake.wiki_utils import (
    SYSTEM_WHITELIST,
    get_memory_dir,
    get_wiki_dir,
    normalize_raw_ref,
    normalize_sources,
    read_markdown_file,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
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


def delete_source(raw_path: str, dry_run: bool = True) -> str:
    wiki_dir = Path(get_wiki_dir()).resolve()
    memory_dir = Path(get_memory_dir()).resolve()
    raw_memory_dir = (memory_dir / "raw").resolve()

    raw_path_obj = Path(raw_path).resolve()
    if not raw_path_obj.is_relative_to(raw_memory_dir):
        return f"[Security Error] The target '{raw_path}' is not within {raw_memory_dir}. Only raw sources can be deleted this way."

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

        filepath = os.path.join(wiki_dir, filename)
        try:
            frontmatter, body, content = read_markdown_file(filepath)
        except Exception as exc:
            scan_failures.append(
                f"{filename}: cannot parse YAML frontmatter: "
                f"{_compact_exception(exc)}"
            )
            continue
        integrity_error = _frontmatter_integrity_error(content, frontmatter)
        if integrity_error:
            scan_failures.append(f"{filename}: {integrity_error}")
            continue

        sources = normalize_sources(frontmatter.get("sources", []))
        source_identities = [
            _source_ref_identity(source) for source in sources
        ]
        has_source_ref = raw_ref_identity in source_identities
        is_source_page = (
            filename.casefold() == expected_source_filename
            or (
                str(frontmatter.get("type") or "").casefold() == "source"
                and has_source_ref
            )
        )
        if not (is_source_page or has_source_ref):
            continue

        if len(sources) <= 1 or is_source_page:
            actions.append(("DELETE", filepath, filename, None, None))
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
                )
            )

    lines = [f"[CASCADE DELETE] Requested raw source: {raw_path_obj}"]
    if os.path.exists(raw_path_obj):
        lines.append(f"  [DELETE_RAW] {raw_path_obj}")
    else:
        lines.append(f"  [MISSING_RAW] {raw_path_obj}")

    if actions:
        lines.append(f"  [WIKI] {len(actions)} affected wiki page(s):")
        for action, _, detail, _, _ in actions:
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
        lines.append("(Dry run — no changes made. Re-run with dry_run=False to execute.)")
        return "\n".join(lines)

    deleted = 0
    updated = 0
    failures = []
    
    from vector_lake.mutation_coordinator import execute_mutation_batch

    mutations = []
    for action, filepath, _, frontmatter, body in actions:
        filename = os.path.basename(filepath)
        if action == "DELETE":
            mutations.append({"filename": filename, "is_delete": True})
            deleted += 1
        elif action == "REMOVE_REF":
            fm_str = yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)
            mutations.append({"filename": filename, "content": f"---\n{fm_str}---\n{body}"})
            updated += 1

    if mutations:
        try:
            execute_mutation_batch(mutations)
        except Exception as exc:
            failures.append(f"ATOMIC_WIKI_CLEANUP: {exc}")
            deleted = 0
            updated = 0
            log.warning("Atomic wiki cleanup failed: %s", exc)

    raw_deleted = False
    if failures:
        log.warning("Skipping raw source deletion because wiki cleanup had failures.")
    elif os.path.exists(raw_path_obj):
        try:
            os.remove(raw_path_obj)
            raw_deleted = True
            log.info("Deleted raw source: %s", raw_path_obj)
        except Exception as e:
            log.warning("Failed to delete raw source %s: %s", raw_path_obj, e)

    lines.append("")
    lines.append(f"Executed: raw_deleted={raw_deleted}, wiki_deleted={deleted}, wiki_updated={updated}. Projection updates queued transactionally.")
    if failures:
        lines.append("Warnings:")
        for failure in failures:
            lines.append(f"  {failure}")
        lines.append("Raw source was preserved because wiki cleanup did not complete successfully.")
    return "\n".join(lines)

