import os
import re
from collections import defaultdict

wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"
source_map = defaultdict(list)

for f in os.listdir(wiki_dir):
    if f.startswith("Source_") and f.endswith(".md"):
        filepath = os.path.join(wiki_dir, f)
        with open(filepath, "r", encoding="utf-8") as fh:
            content = fh.read()
        match = re.search(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if match:
            yaml_block = match.group(1)
            src_match = re.search(r"sources:\s*\[(.*?)\]", yaml_block, re.DOTALL)
            if src_match:
                sources_str = src_match.group(1)
                sources = [s.strip().strip('"').strip("'") for s in sources_str.split(",")]
                for src in sources:
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

# Also find Source pages with similar names
from difflib import SequenceMatcher
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
