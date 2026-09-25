#!/usr/bin/env python3
"""Reorder Synthesis_* pages so that the mandatory skeleton headings open the document.

Required order per schema.md:
  ## 核心合成论点 (Core Synthesized Claims)
  ## 支撑拓扑 (Supporting Topology)

Writes proceed through execute_mutation_batch to guarantee atomic validation and change tracking.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from vector_lake.mutation_coordinator import execute_mutation_batch
from vector_lake.schema_validator import (
    SYNTHESIS_SKELETON_HEADINGS,
    synthesis_skeleton_order_report,
)
from vector_lake.wiki_utils import get_meta_dir, get_wiki_dir, read_markdown_file
from vector_lake.yaml_utils import dump_yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("reorder_synthesis_skeleton")

_H1_PATTERN = re.compile(r"^(#\s+[^\n]+)", re.M)


def fix_synthesis_content(body: str) -> str | None:
    """Extract skeleton sections and move them directly after H1 title."""
    h1 = SYNTHESIS_SKELETON_HEADINGS[0]
    h2 = SYNTHESIS_SKELETON_HEADINGS[1]

    if h1 not in body or h2 not in body:
        return None

    # Check if already leading
    h2_lines = [l.strip() for l in body.splitlines() if l.startswith("## ")]
    if len(h2_lines) >= 2 and tuple(h2_lines[:2]) == SYNTHESIS_SKELETON_HEADINGS:
        return None

    lines = body.splitlines(keepends=True)
    h1_idx = None
    h2_idx = None
    next_h2_idx = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == h1:
            h1_idx = i
        elif stripped == h2 and h1_idx is not None and h2_idx is None:
            h2_idx = i
        elif stripped.startswith("## ") and h2_idx is not None and next_h2_idx is None:
            next_h2_idx = i

    if h1_idx is None or h2_idx is None:
        return None

    end_idx = next_h2_idx if next_h2_idx is not None else len(lines)
    skeleton_chunk = "".join(lines[h1_idx:end_idx]).strip()

    # Remaining lines without the skeleton
    remaining = "".join(lines[:h1_idx] + lines[end_idx:]).strip()

    # Find the # H1 title in body and ensure it is at top
    h1_match = _H1_PATTERN.search(remaining)
    if h1_match:
        h1_line = h1_match.group(1).strip()
        remaining_without_h1 = (remaining[:h1_match.start()] + "\n" + remaining[h1_match.end():]).strip()
        new_body = (
            h1_line
            + "\n\n"
            + skeleton_chunk
            + "\n\n"
            + remaining_without_h1
            + "\n"
        )
    else:
        new_body = skeleton_chunk + "\n\n" + remaining + "\n"

    # Verify order is fixed
    if synthesis_skeleton_order_report(new_body) is None:
        return new_body

    return None


def collect_reorder_plans() -> list[dict]:
    wiki_dir = get_wiki_dir()
    plans = []
    for path in sorted(wiki_dir.glob("Synthesis_*.md")):
        frontmatter, body, full_text = read_markdown_file(path)
        err = synthesis_skeleton_order_report(body)
        if not err:
            continue
        new_body = fix_synthesis_content(body)
        if new_body is not None:
            yaml_block = dump_yaml(frontmatter, allow_unicode=True, default_flow_style=False, sort_keys=False)
            new_full_content = f"---\n{yaml_block}---\n{new_body.lstrip()}"
            plans.append({
                "filename": path.name,
                "old_content": full_text,
                "new_content": new_full_content,
            })
        else:
            log.warning("Could not auto-reorder skeleton for %s", path.name)
    return plans


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply mutations (default is dry-run)")
    args = parser.parse_args()

    plans = collect_reorder_plans()
    print(f"Found {len(plans)} Synthesis file(s) to reorder:")
    for item in plans:
        print(f"  - {item['filename']}")

    if not plans:
        print("All Synthesis files are compliant.")
        return 0

    if not args.apply:
        print("\n[DRY RUN] Run with --apply to commit mutations through MutationCoordinator.")
        return 0

    meta_dir = get_meta_dir()
    migrations_dir = meta_dir / "migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    rollback_file = migrations_dir / f"reorder-synthesis-skeleton-{today}.rollback.jsonl"

    with open(rollback_file, "w", encoding="utf-8") as handle:
        for item in plans:
            record = {
                "filename": item["filename"],
                "content": item["old_content"],
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"Rollback log written to {rollback_file}")

    mutations = [
        {"filename": item["filename"], "content": item["new_content"]}
        for item in plans
    ]

    execute_mutation_batch(mutations, validation_mode="schema")
    print(f"Successfully reordered skeleton for all {len(plans)} Synthesis pages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
