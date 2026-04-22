import os
wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"
files = os.listdir(wiki_dir)
for f in files:
    if "MSL" in f or "Agentic" in f:
        print(f)
