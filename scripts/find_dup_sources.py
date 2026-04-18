"""
Purpose:
    Ad hoc report for duplicated raw->Source mappings and near-duplicate Source page names.

Safe to delete:
    Yes, if equivalent checks are folded into a permanent governance or lint command.

Replaced by CLI:
    Partially. Current lint/governance commands cover related classes but not this exact report.
"""

import os
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vector_lake.wiki_utils import get_wiki_dir, normalize_sources, read_markdown_file

wiki_dir = str(get_wiki_dir())
source_map = defaultdict(list)

for f in os.listdir(wiki_dir):
    if f.startswith("Source_") and f.endswith(".md"):
        filepath = os.path.join(wiki_dir, f)
        try:
            frontmatter, _, _ = read_markdown_file(filepath)
        except Exception:
            continue
        for src in normalize_sources(frontmatter.get("sources", [])):
            if src:
                source_map[src].append(f)

output = []
output.append("=== Raw files with multiple Source wiki pages ===")
dup_count = 0
for raw, wikis in sorted(source_map.items()):
    if len(wikis) > 1:
        dup_count += 1
        output.append(f"\n[DUP {dup_count}] Raw: {raw}")
        for w in wikis:
            output.append(f"  -> {w}")

output.append(f"\n\n=== Stats ===")
output.append(f"Total unique raw file references: {len(source_map)}")
output.append(f"Raw files with duplicates: {dup_count}")

source_files = sorted([f for f in os.listdir(wiki_dir) if f.startswith("Source_") and f.endswith(".md")])
output.append(f"\n\n=== Name-similar Source pages (ratio > 0.75) ===")
sim_count = 0
for i, f1 in enumerate(source_files):
    for f2 in source_files[i+1:]:
        ratio = SequenceMatcher(None, f1, f2).ratio()
        if ratio > 0.75:
            sim_count += 1
            output.append(f"\n[SIM {sim_count}] ratio={ratio:.2f}")
            output.append(f"  A: {f1}")
            output.append(f"  B: {f2}")

report_path = os.path.join(os.path.dirname(__file__), "dup_report.txt")
with open(report_path, "w", encoding="utf-8") as f:
    f.write("\n".join(output))
print(f"Report written to {report_path}")
print(f"Found {dup_count} raw-file duplicates and {sim_count} name-similar pairs.")
