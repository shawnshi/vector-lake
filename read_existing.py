import os
import json

wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"
files_to_check = [
    "Entity_卫宁健康.md",
    "Entity_Epic_Systems.md",
    "Entity_WiNEX.md",
    "Entity_EpicOps.md",
    "Concept_Agent_Harness.md",
    "Concept_Agentic_Integration_Abyss.md"
]
res = {}
for f in files_to_check:
    path = os.path.join(wiki_dir, f)
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as file:
            res[f] = file.read()
    else:
        res[f] = None
print(json.dumps(res, ensure_ascii=False))