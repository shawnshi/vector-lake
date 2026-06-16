import os
import re

wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"
alias_map = {}
file_contents = {}

for f in os.listdir(wiki_dir):
    if not f.endswith(".md"): continue
    p = os.path.join(wiki_dir, f)
    try:
        with open(p, "r", encoding="utf-8") as file:
            content = file.read()
            file_contents[f] = content
            
            # Find aliases: [...] or aliases: \n  - ...
            from vector_lake.yaml_utils import load_yaml, dump_yaml
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    fm = load_yaml(parts[1])
                    if fm and "aliases" in fm:
                        aliases = fm["aliases"]
                        if isinstance(aliases, str): aliases = [aliases]
                        if isinstance(aliases, list):
                            for a in aliases:
                                if a: alias_map.setdefault(str(a).strip(), []).append(f)
    except Exception as e:
        print(f"Error reading {f}: {e}")

conflicts = {a: fs for a, fs in alias_map.items() if len(set(fs)) > 1}
print(f"Found {len(conflicts)} alias conflicts.")

for alias, fs in conflicts.items():
    fs = list(set(fs))
    # Keep the file where the filename is closest to the alias
    fs.sort(key=lambda x: 100 if x[:-3] == alias else (50 if alias in x else 0), reverse=True)
    winner = fs[0]
    losers = fs[1:]
    
    for loser in losers:
        p = os.path.join(wiki_dir, loser)
        try:
            with open(p, "r", encoding="utf-8") as file:
                content = file.read()
            if not content.startswith("---"): continue
            parts = content.split("---", 2)
            if len(parts) < 3: continue
            
            fm = load_yaml(parts[1])
            if fm and "aliases" in fm:
                aliases = fm["aliases"]
                if isinstance(aliases, str): aliases = [aliases]
                if isinstance(aliases, list) and alias in aliases:
                    aliases.remove(alias)
                    fm["aliases"] = aliases
                    
                    with open(p, "w", encoding="utf-8") as file:
                        file.write("---\n")
                        dump_yaml(fm, file, allow_unicode=True, sort_keys=False, default_flow_style=False)
                        file.write("---")
                        file.write(parts[2])
                    print(f"Removed alias '{alias}' from {loser}")
        except Exception as e:
            print(f"Error updating {loser}: {e}")
