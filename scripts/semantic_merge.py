import sys
import os
import re
import yaml

def load_yaml(yaml_str):
    try:
        return yaml.safe_load(yaml_str)
    except yaml.YAMLError:
        return {}

def merge_markdown_files(left_path, right_path):
    with open(left_path, 'r', encoding='utf-8') as f:
        left_content = f.read()
    with open(right_path, 'r', encoding='utf-8') as f:
        right_content = f.read()

    # Parse right
    right_fm_match = re.search(r"^---\n(.*?)\n---", right_content, re.MULTILINE | re.DOTALL)
    right_fm_str = right_fm_match.group(1) if right_fm_match else ""
    right_fm = load_yaml(right_fm_str) or {}
    right_body = right_content[right_fm_match.end():].strip() if right_fm_match else right_content.strip()
    right_aliases = right_fm.get("aliases") or []
    if not isinstance(right_aliases, list):
        right_aliases = [right_aliases]
        
    # Also add the original title of right to aliases
    right_title = right_fm.get("title", "")
    if right_title and right_title not in right_aliases:
        right_aliases.append(right_title)

    # Parse left
    left_fm_match = re.search(r"^---\n(.*?)\n---", left_content, re.MULTILINE | re.DOTALL)
    if not left_fm_match:
        print(f"Error: {left_path} has no valid frontmatter.")
        sys.exit(1)
        
    left_fm_str = left_fm_match.group(1)
    left_fm = load_yaml(left_fm_str) or {}
    left_body = left_content[left_fm_match.end():].strip()
    
    left_aliases = left_fm.get("aliases") or []
    if not isinstance(left_aliases, list):
        left_aliases = [left_aliases]
        
    # Merge aliases
    for alias in right_aliases:
        if alias and alias not in left_aliases:
            left_aliases.append(alias)
            
    left_fm["aliases"] = left_aliases

    # Reconstruct left markdown
    new_fm_str = yaml.dump(left_fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
    new_left_content = f"---\n{new_fm_str}---\n{left_body}\n\n## Merged from {right_title}\n{right_body}\n"

    # Write left
    with open(left_path, 'w', encoding='utf-8') as f:
        f.write(new_left_content)

    # Delete right
    os.remove(right_path)
    print(f"Merged {right_path} into {left_path} and deleted right.")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python semantic_merge.py <left_path> <right_path>")
        sys.exit(1)
        
    left_path = sys.argv[1]
    right_path = sys.argv[2]
    
    if not os.path.exists(left_path):
        print(f"Error: {left_path} does not exist.")
        sys.exit(1)
    if not os.path.exists(right_path):
        print(f"Error: {right_path} does not exist.")
        sys.exit(1)
        
    merge_markdown_files(left_path, right_path)
