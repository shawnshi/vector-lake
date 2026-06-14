import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from vector_lake.yaml_utils import load_yaml, dump_yaml

wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"

def merge(winner_name, loser_name):
    winner_path = os.path.join(wiki_dir, winner_name)
    loser_path = os.path.join(wiki_dir, loser_name)
    
    if not os.path.exists(winner_path):
        print(f"Skipping: Winner {winner_name} not found")
        return
    if not os.path.exists(loser_path):
        print(f"Skipping: Loser {loser_name} not found")
        return
        
    with open(winner_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
        
    parts = content.split("---", 2)
    fm = load_yaml(parts[1])
    
    # Ensure aliases list exists
    if "aliases" not in fm or fm["aliases"] is None:
        fm["aliases"] = []
    elif isinstance(fm["aliases"], str):
        fm["aliases"] = [fm["aliases"]]
        
    loser_base = loser_name[:-3]
    if loser_base not in fm["aliases"]:
        fm["aliases"].append(loser_base)
        
    with open(winner_path, "w", encoding="utf-8") as f:
        f.write("---\n")
        dump_yaml(fm, stream=f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        f.write("---")
        f.write(parts[2])
        
    # Delete the loser
    os.remove(loser_path)
    print(f"Merged '{loser_name}' into '{winner_name}'")

pairs = [
    ("Concept_临床合理推断_(Clinical_Inference).md", "Concept_临床合理推断 (Clinical Inference).md"),
    ("Concept_算力热力学_(Computing_Thermodynamics).md", "Concept_算力热力学_(Compute_Thermodynamics).md"),
    ("Source_20250708刘海一主任沟通山东项目文审材料.md", "Source_20250708刘主任沟通山东项目文审材料.md")
]

for winner, loser in pairs:
    merge(winner, loser)
