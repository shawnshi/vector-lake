import os
import json

wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"
files = [
    "Source_research_abundance_flywheel_root_node_20260419.md",
    "Source_research_medical_federation_edge_agent_20260419.md",
    "Entity_Demis_Hassabis.md",
    "Entity_AlphaFold.md",
    "Concept_Abundance_Flywheel.md",
    "Concept_Root_Node_Problems.md",
    "Concept_Edge_AI_Agents.md"
]

res = {}
for f in files:
    path = os.path.join(wiki_dir, f)
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as file:
            res[f] = file.read()
    else:
        res[f] = None

print(json.dumps(res, ensure_ascii=False))
