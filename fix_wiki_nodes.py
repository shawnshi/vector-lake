import os
import re
import yaml

wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"

def fix_node(filename):
    path = os.path.join(wiki_dir, filename)
    if not os.path.exists(path):
        return
    
    with open(path, 'rb') as f:
        binary_content = f.read()
    
    # Try to decode as utf-8, handle BOMs
    try:
        content = binary_content.decode('utf-8-sig')
    except:
        content = binary_content.decode('gbk', errors='replace')
    
    # Remove BOMs that might be in the middle of the string
    content = content.replace('\ufeff', '')
    
    # Find all frontmatter blocks
    blocks = re.findall(r'---\s*\n(.*?)\n---', content, re.DOTALL)
    if len(blocks) < 1:
        return
    
    # Merge frontmatter
    merged_fm = {}
    for block in blocks:
        try:
            fm = yaml.safe_load(block)
            if isinstance(fm, dict):
                merged_fm.update(fm)
        except:
            pass
    
    # Extract body (everything after the last ---)
    # Let's find the last occurrence of ---
    last_idx = content.rfind('---')
    if last_idx != -1:
        # Find the end of that block
        # Actually, if we have ---fm--- ---fm--- body
        # the last --- might be the end of the second block.
        # Let's use the regex to find all ends.
        matches = list(re.finditer(r'---\s*\n', content))
        if len(matches) >= 2:
            # We want everything after the 2nd match if it's the end of a block, 
            # or after the 4th match if it's the end of the 2nd block...
            # A block starts with --- and ends with ---.
            # So if we have N blocks, we have 2N '---' lines.
            # The body starts after the 2N-th '---'.
            
            # Simplified approach:
            body = re.sub(r'---\s*\n.*?\n---\s*\n?', '', content, flags=re.DOTALL).strip()
    else:
        body = content.strip()
    
    # Reconstruct
    new_fm_str = yaml.dump(merged_fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
    new_content = f"---\n{new_fm_str}---\n\n{body}\n"
    
    with open(path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    print(f"Fixed {filename}")

for filename in os.listdir(wiki_dir):
    if filename.endswith(".md"):
        fix_node(filename)
