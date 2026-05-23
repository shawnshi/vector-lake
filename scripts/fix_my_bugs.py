import os
import yaml
# Use C-based PyYAML extensions for a massive parsing/dumping speedup, with pure Python fallback.
try:
    from yaml import CSafeLoader as SafeLoader, CDumper as Dumper
except ImportError:
    from yaml import SafeLoader, Dumper

wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"
count = 0

for filename in os.listdir(wiki_dir):
    if not filename.endswith(".md"): continue
    filepath = os.path.join(wiki_dir, filename)
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.startswith("---"): continue
        parts = content.split("---", 2)
        if len(parts) < 3: continue
        
        fm = yaml.load(parts[1], Loader=SafeLoader)
        changed = False
        
        # Capitalize type
        if fm and "type" in fm:
            old_type = fm["type"]
            if isinstance(old_type, str) and len(old_type) > 0 and old_type[0].islower():
                fm["type"] = old_type.capitalize()
                changed = True
                
        # Fix epistemic-status
        if fm and "epistemic-status" in fm:
            if fm["epistemic-status"] == "active":
                fm["epistemic-status"] = "Active"
                changed = True
        
        # Deduplicate aliases within the same file
        if fm and "aliases" in fm:
            aliases = fm["aliases"]
            if isinstance(aliases, list):
                seen = set()
                new_aliases = []
                for a in aliases:
                    if a not in seen:
                        seen.add(a)
                        new_aliases.append(a)
                if len(new_aliases) < len(aliases):
                    fm["aliases"] = new_aliases
                    changed = True

        if changed:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write("---\n")
                yaml.dump(fm, f, Dumper=Dumper, allow_unicode=True, sort_keys=False, default_flow_style=False)
                f.write("---")
                f.write(parts[2])
            count += 1
            print(f"Fixed {filename}")
    except Exception as e:
        pass

print(f"Total fixed: {count}")
