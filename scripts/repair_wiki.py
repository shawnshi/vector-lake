"""
Vector Lake Wiki Batch Repair Script
=====================================
Fixes P0-1 (Duplicate IDs) and P1 (Invalid epistemic-status) in a single pass.

Purpose:
    Ad hoc maintenance utility for wiki frontmatter normalization.

Safe to delete:
    Yes, after the equivalent checks are permanently covered by CLI lint/fix flows.

Replaced by CLI:
    Partially. `python cli.py lint --auto-fix` overlaps with parts of this script.

Usage:
    python scripts/repair_wiki.py          # Dry-run (report only)
    python scripts/repair_wiki.py --apply  # Apply changes
"""

import os
import re
import sys
import yaml
import random
import string
from pathlib import Path
from collections import defaultdict
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from vector_lake.wiki_utils import get_wiki_dir


WIKI_DIR = str(get_wiki_dir())

# --- P1: epistemic-status normalization map ---
EPISTEMIC_NORMALIZE = {
    "final": "evergreen",
    "research_report": "evergreen",
    "published": "evergreen",
    "processed": "evergreen",
    "strategic_insight": "evergreen",
}

VALID_EPISTEMIC = {"seed", "sprouting", "evergreen", "deprecated"}


def parse_frontmatter(content):
    """Returns (fm_data: dict or None, fm_match: re.Match or None)"""
    m = re.search(r'^---\n(.*?)\n---', content, re.MULTILINE | re.DOTALL)
    if not m:
        return None, None
    try:
        data = yaml.safe_load(m.group(1))
        if isinstance(data, dict):
            return data, m
    except yaml.YAMLError:
        pass
    return None, None


def rebuild_content(content, fm_data, fm_match):
    """Rebuild file content with modified frontmatter, preserving body."""
    after_fm = content.split('---', 2)[2]
    new_fm = yaml.dump(fm_data, allow_unicode=True, default_flow_style=False).strip()
    return "---\n" + new_fm + "\n---" + after_fm


def generate_unique_id(created_date, used_ids):
    """Generate a unique ID based on created date + 4-char random suffix."""
    if created_date:
        try:
            dt = datetime.strptime(str(created_date), "%Y-%m-%d")
            base = dt.strftime("%Y%m%d")
        except (ValueError, TypeError):
            base = datetime.now().strftime("%Y%m%d")
    else:
        base = datetime.now().strftime("%Y%m%d")

    for _ in range(100):
        suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
        candidate = f"{base}_{suffix}"
        if candidate not in used_ids:
            used_ids.add(candidate)
            return candidate
    # Fallback: use full timestamp + random
    fallback = datetime.now().strftime("%Y%m%d%H%M%S") + ''.join(random.choices(string.digits, k=4))
    used_ids.add(fallback)
    return fallback


def main():
    apply_mode = "--apply" in sys.argv
    mode_label = "APPLY" if apply_mode else "DRY-RUN"
    print(f"{'='*60}")
    print(f" Vector Lake Wiki Repair [{mode_label}]")
    print(f"{'='*60}")

    if not os.path.exists(WIKI_DIR):
        print("ERROR: Wiki directory not found.")
        return

    # --- Pass 1: Collect all IDs to find duplicates ---
    id_to_files = defaultdict(list)  # id -> [filename, ...]
    file_data = {}                    # filename -> (content, fm_data, fm_match)

    filenames = [f for f in os.listdir(WIKI_DIR) if f.endswith(".md") and f not in ("index.md", "log.md")]

    for filename in filenames:
        filepath = os.path.join(WIKI_DIR, filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        except (UnicodeDecodeError, OSError):
            continue

        fm_data, fm_match = parse_frontmatter(content)
        if fm_data is None:
            continue

        file_data[filename] = (content, fm_data, fm_match)
        node_id = str(fm_data.get("id", "")).strip()
        if node_id:
            id_to_files[node_id].append(filename)

    # --- Identify duplicates (keep first, fix rest) ---
    all_used_ids = set(id_to_files.keys())
    dup_count = 0
    id_fixes = {}  # filename -> new_id

    for old_id, files in id_to_files.items():
        if len(files) <= 1:
            continue
        # Keep first file's ID, regenerate for the rest
        for dup_file in files[1:]:
            content, fm_data, fm_match = file_data[dup_file]
            created = fm_data.get("created", "")
            new_id = generate_unique_id(created, all_used_ids)
            id_fixes[dup_file] = new_id
            dup_count += 1

    # --- Pass 2: Fix epistemic-status ---
    epistemic_fixes = {}  # filename -> (old_val, new_val)

    for filename, (content, fm_data, fm_match) in file_data.items():
        status = str(fm_data.get("epistemic-status", "")).strip()
        status_lower = status.lower()
        if status_lower in EPISTEMIC_NORMALIZE:
            epistemic_fixes[filename] = (status, EPISTEMIC_NORMALIZE[status_lower])

    # --- Report ---
    print(f"\n[P0-1] Duplicate IDs to fix: {dup_count}")
    for fname, new_id in sorted(id_fixes.items())[:20]:
        content, fm_data, _ = file_data[fname]
        old_id = fm_data.get("id", "?")
        print(f"    {fname}: '{old_id}' -> '{new_id}'")
    if len(id_fixes) > 20:
        print(f"    ... and {len(id_fixes) - 20} more.")

    print(f"\n[P1] Invalid epistemic-status to fix: {len(epistemic_fixes)}")
    for fname, (old_v, new_v) in sorted(epistemic_fixes.items()):
        print(f"    {fname}: '{old_v}' -> '{new_v}'")

    total_files_modified = set(id_fixes.keys()) | set(epistemic_fixes.keys())
    print(f"\nTotal files to modify: {len(total_files_modified)}")

    if not apply_mode:
        print(f"\n⚠️  DRY-RUN complete. Re-run with --apply to execute changes.")
        return

    # --- Apply fixes ---
    print(f"\nApplying fixes...")
    modified = 0
    for filename in total_files_modified:
        content, fm_data, fm_match = file_data[filename]
        changed = False

        if filename in id_fixes:
            fm_data["id"] = id_fixes[filename]
            changed = True

        if filename in epistemic_fixes:
            fm_data["epistemic-status"] = epistemic_fixes[filename][1]
            changed = True

        if changed:
            filepath = os.path.join(WIKI_DIR, filename)
            new_content = rebuild_content(content, fm_data, fm_match)
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(new_content)
            modified += 1

    print(f"\n✅ Applied fixes to {modified} files.")
    print(f"   - {dup_count} duplicate IDs regenerated")
    print(f"   - {len(epistemic_fixes)} epistemic-status values normalized")
    print(f"\nRun 'python cli.py lint' to verify.")


if __name__ == "__main__":
    main()
