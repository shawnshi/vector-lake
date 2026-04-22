import os
import json

wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"

def read_if_exists(filename):
    path = os.path.join(wiki_dir, filename)
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    return None

overview = read_if_exists("overview.md")
index = read_if_exists("index.json")

print("OVERVIEW_START")
print(overview)
print("OVERVIEW_END")

# For index, maybe just first 1000 chars to avoid token blowup
if index:
    print("INDEX_START")
    print(index[:2000])
    print("INDEX_END")
else:
    print("INDEX_NOT_FOUND")
