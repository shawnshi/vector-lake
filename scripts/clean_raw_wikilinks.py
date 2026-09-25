#!/usr/bin/env python3
"""Normalize pseudo-wikilinks like [[raw/...]] into standard source references raw/...

Wiki links [[Target]] are strictly for inter-page navigation between markdown entities.
Physical filepaths under raw/ should never be wrapped in [[...]].
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from vector_lake.mutation_coordinator import execute_mutation_batch
from vector_lake.wiki_utils import get_meta_dir, get_wiki_dir, read_markdown_file
from vector_lake.yaml_utils import dump_yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("clean_raw_wikilinks")

# Pattern matching [[raw/...]] or [[raw/...|...]]
_RAW_WIKILINK_PATTERN = re.compile(r"\[\[(raw/[^\]|#\n]+)(?:\|([^\]\n]+))?\]\]")


def clean_body_raw_links(body: str) -> tuple[str, list[tuple[str, str]]]:
    replaced = []

    def replacer(match: re.Match) -> str:
        raw_path = match.group(1).strip()
        alias = match.group(2)
        target = alias.strip() if alias else raw_path
        replaced.append((match.group(0), target))
        return target

    new_body = _RAW_WIKILINK_PATTERN.sub(replacer, body)
    return new_body, replaced


def collect_plans() -> list[dict]:
    wiki_dir = get_wiki_dir()
    plans = []
    for path in sorted(wiki_dir.glob("*.md")):
        if path.name in {"index.md", "log.md", "overview.md"}:
            continue
        frontmatter, body, full_text = read_markdown_file(path)
        new_body, replaced = clean_body_raw_links(body)
        if replaced:
            yaml_block = dump_yaml(frontmatter, allow_unicode=True, default_flow_style=False, sort_keys=False)
            new_full = f"---\n{yaml_block}---\n{new_body.lstrip()}"
            plans.append({
                "filename": path.name,
                "old_content": full_text,
                "new_content": new_full,
                "replaced": replaced,
            })
    return plans


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply mutations (default is dry-run)")
    args = parser.parse_args()

    plans = collect_plans()
    total_links = sum(len(p["replaced"]) for p in plans)
    print(f"Found {len(plans)} page(s) containing {total_links} pseudo [[raw/...]] link(s):")
    for p in plans[:15]:
        print(f"  - {p['filename']} ({len(p['replaced'])} links)")

    if not plans:
        print("No pseudo [[raw/...]] links found.")
        return 0

    if not args.apply:
        print("\n[DRY RUN] Run with --apply to commit cleanups through MutationCoordinator.")
        return 0

    meta_dir = get_meta_dir()
    migrations_dir = meta_dir / "migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    rollback_file = migrations_dir / f"clean-raw-wikilinks-{today}.rollback.jsonl"

    with open(rollback_file, "w", encoding="utf-8") as handle:
        for item in plans:
            record = {
                "filename": item["filename"],
                "content": item["old_content"],
                "replaced": item["replaced"],
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"Rollback log written to {rollback_file}")

    mutations = [
        {"filename": item["filename"], "content": item["new_content"]}
        for item in plans
    ]

    for i in range(0, len(mutations), 30):
        chunk = mutations[i : i + 30]
        execute_mutation_batch(chunk, validation_mode="schema")
        print(f"Committed {min(i + 30, len(mutations))}/{len(mutations)} pages...")

    print(f"Successfully cleaned {total_links} pseudo-links across {len(plans)} pages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
