import os
import re
import yaml
from datetime import datetime
import difflib
import argparse

WIKI_DIR = os.path.join(os.path.expanduser("~"), ".gemini", "MEMORY", "wiki")

def main():
    parser = argparse.ArgumentParser(description="Migrate Vector Lake V5.0 schema to V6.0")
    parser.add_argument('--apply', action='store_true', help="Apply modifications to files instead of dry-run")
    args = parser.parse_args()

    dry_run = not args.apply
    
    if not os.path.exists(WIKI_DIR):
        print(f"Wiki directory not found at {WIKI_DIR}")
        return

    log_file = None
    if dry_run:
        log_file = open(os.path.join(os.path.dirname(__file__), "..", "dry_run_log.txt"), "w", encoding="utf-8")

    print("="*40)
    print(f"Vector Lake Schema Migration")
    print(f"Mode: {'APPLY (WRITE)' if args.apply else 'DRY RUN'}")
    print("="*40)

    files_modified = 0
    for filename in os.listdir(WIKI_DIR):
        if not filename.endswith(".md") or filename in ("index.md", "log.md"):
            continue
        
        filepath = os.path.join(WIKI_DIR, filename)
        
        modified = False
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        frontmatter_match = re.search(r'^---\n(.*?)\n---', content, re.MULTILINE | re.DOTALL)
        if not frontmatter_match:
            continue

        fm_str = frontmatter_match.group(1)
        try:
            fm_data = yaml.safe_load(fm_str) or {}
        except yaml.YAMLError:
            continue

        if 'id' not in fm_data:
            created_raw = fm_data.get('created', '')
            created_str = str(created_raw) if created_raw else ''
            if created_str and re.match(r'\d{4}-\d{2}-\d{2}', created_str):
                try:
                    dt = datetime.strptime(created_str, "%Y-%m-%d")
                    fm_data['id'] = dt.strftime("%Y%m%d000000")
                except ValueError:
                     stat = os.stat(filepath)
                     fm_data['id'] = datetime.fromtimestamp(stat.st_ctime).strftime("%Y%m%d%H%M%S")
            else:
                stat = os.stat(filepath)
                fm_data['id'] = datetime.fromtimestamp(stat.st_ctime).strftime("%Y%m%d%H%M%S")
            modified = True

        if 'categories' not in fm_data:
            fm_data['categories'] = ["Uncategorized"]
            modified = True

        new_fm_str = yaml.dump(fm_data, allow_unicode=True, default_flow_style=False, sort_keys=False)
        new_content = content[:frontmatter_match.start()] + "---\n" + new_fm_str.strip() + "\n---" + content[frontmatter_match.end():]

        link_pattern = re.compile(r'\[\[([^:\]]*?)::([^\]]*?)(?:\.md)?\|?([^\]]*?)\]\]')
        def link_replacer(match):
            relation = match.group(1).strip()
            target = match.group(2).strip()
            return f"[{relation}:: [[{target}]]]"

        migrated_content, count = link_pattern.subn(link_replacer, new_content)
        if count > 0:
            modified = True

        if modified:
            if dry_run:
                if log_file:
                    log_file.write(f"--- Diff for {os.path.basename(filepath)} ---\n")
                    diff = difflib.unified_diff(
                        content.splitlines(keepends=True),
                        migrated_content.splitlines(keepends=True),
                        fromfile=os.path.basename(filepath),
                        tofile=os.path.basename(filepath) + " (V6.0)"
                    )
                    log_file.write(''.join(list(diff)[:15]) + "\n\n")
            else:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(migrated_content)
            files_modified += 1

    print(f"\nMigration complete. Files modified: {files_modified}.")
    if log_file:
        log_file.close()

if __name__ == "__main__":
    main()
