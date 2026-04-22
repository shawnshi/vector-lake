import os

wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"

def reencode_to_utf8(filename):
    path = os.path.join(wiki_dir, filename)
    if not os.path.exists(path):
        return
    
    with open(path, 'rb') as f:
        binary_content = f.read()
    
    # Try to decode from various encodings
    for encoding in ['utf-8-sig', 'utf-8', 'gbk', 'utf-16']:
        try:
            content = binary_content.decode(encoding)
            # Remove any remaining BOMs
            content = content.replace('\ufeff', '')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
            print(f"Re-encoded {filename} using {encoding}")
            return
        except:
            continue
    print(f"Failed to re-encode {filename}")

reencode_to_utf8("overview.md")
reencode_to_utf8("log.md")
