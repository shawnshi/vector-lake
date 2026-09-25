#!/usr/bin/env python3
"""Repoint declared raw paths whose file was moved to a new subdirectory under raw/.

Only exact matches with a globally unique basename in the raw tree are repointed.
Writes proceed through execute_mutation_batch to keep SQLite sources and Markdown in step.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from vector_lake.mutation_coordinator import execute_mutation_batch
from vector_lake.wiki_utils import get_meta_dir, get_raw_dir, get_wiki_dir, read_markdown_file

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("repoint_moved_sources")


def find_fixable_moves() -> list[dict]:
    raw_root = get_raw_dir()
    memory_root = raw_root.parent
    wiki_root = get_wiki_dir()

    by_basename: dict[str, list[str]] = defaultdict(list)
    for candidate in raw_root.rglob("*"):
        if candidate.is_file():
            by_basename[candidate.name].append(candidate.relative_to(memory_root).as_posix())

    pages_to_update: dict[str, dict] = {}

    for path in sorted(wiki_root.glob("*.md")):
        frontmatter, body, full_text = read_markdown_file(path)
        sources = frontmatter.get("sources") or []
        if isinstance(sources, str):
            sources = [sources]

        changed = False
        new_sources = []
        replaced_pairs = []

        for entry in sources:
            ref = str(entry or "").strip().strip("'\"")
            if not ref.startswith("raw/"):
                new_sources.append(entry)
                continue

            full_path = memory_root / ref
            if full_path.exists():
                new_sources.append(ref)
                continue

            candidates = by_basename.get(Path(ref).name, [])
            if len(candidates) == 1:
                new_ref = candidates[0]
                new_sources.append(new_ref)
                replaced_pairs.append((ref, new_ref))
                changed = True
            else:
                new_sources.append(ref)

        if changed:
            # Reconstruct content with updated sources
            updated_fm = dict(frontmatter)
            updated_fm["sources"] = new_sources

            # Update markdown text in frontmatter safely
            from vector_lake.yaml_utils import dump_yaml

            yaml_block = dump_yaml(updated_fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
            new_content = f"---\n{yaml_block}---\n{body.lstrip()}"

            pages_to_update[path.name] = {
                "filename": path.name,
                "old_content": full_text,
                "new_content": new_content,
                "replaced": replaced_pairs,
            }

    return list(pages_to_update.values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply mutations (default is dry-run)")
    args = parser.parse_args()

    moves = find_fixable_moves()
    if not moves:
        print("No moved raw sources found to repoint.")
        return 0

    all_pairs = [pair for item in moves for pair in item["replaced"]]
    pattern_counts = Counter(f"{old.rsplit('/', 1)[0]} -> {new.rsplit('/', 1)[0]}" for old, new in all_pairs)

    print(f"Found {len(moves)} page(s) with {len(all_pairs)} uniquely resolvable raw path move(s):")
    for pat, count in pattern_counts.most_common():
        print(f"  {count:>4}  {pat}")

    if not args.apply:
        print("\n[DRY RUN] Run with --apply to commit mutations through MutationCoordinator.")
        return 0

    # Write rollback log
    meta_dir = get_meta_dir()
    migrations_dir = meta_dir / "migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    rollback_file = migrations_dir / f"repoint-moved-sources-{today}.rollback.jsonl"

    with open(rollback_file, "w", encoding="utf-8") as handle:
        for item in moves:
            record = {
                "filename": item["filename"],
                "content": item["old_content"],
                "replaced": item["replaced"],
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"Rollback log written to {rollback_file}")

    mutations = [
        {"filename": item["filename"], "content": item["new_content"]}
        for item in moves
    ]

    # Batch commit via MutationCoordinator in chunks of 50 to avoid huge SQLite transactions
    total_committed = 0
    for i in range(0, len(mutations), 50):
        chunk = mutations[i : i + 50]
        execute_mutation_batch(chunk, validation_mode="schema")
        total_committed += len(chunk)
        print(f"Committed {total_committed}/{len(mutations)} pages...")

    print(f"\nSuccessfully repointed {len(all_pairs)} paths across {len(moves)} pages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
