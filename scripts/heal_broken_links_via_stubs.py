import os
import re
import datetime
from vector_lake.yaml_utils import dump_yaml

wiki_dir = r"C:\Users\shich\.gemini\MEMORY\wiki"

existing_files = {name.replace(".md", "") for name in os.listdir(wiki_dir) if name.endswith(".md")}

broken_targets = set()

for filename in os.listdir(wiki_dir):
    if not filename.endswith(".md"): continue
    filepath = os.path.join(wiki_dir, filename)
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as handle:
            content = handle.read()
            for match in re.finditer(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]", content):
                raw_target = match.group(1).strip().replace(".md", "")
                if raw_target and raw_target not in existing_files:
                    broken_targets.add(raw_target)
    except Exception as e:
        print(f"Error reading {filename}: {e}")

print(f"Found {len(broken_targets)} unique broken link targets.")

stubs = 0
today = datetime.datetime.now().strftime("%Y-%m-%d")
for target in broken_targets:
    node_type = target.split("_")[0].lower() if target.startswith(("Concept_", "Vendor_", "Product_", "Person_", "Event_", "Source_", "Synthesis_")) else "concept"
    frontmatter = {
        "id": f"stub_{target.lower()}",
        "title": target.replace("_", " "),
        "type": node_type,
        "domain": "Uncategorized",
        "topic_cluster": "Uncategorized",
        "status": "Active",
        "epistemic-status": "seed",
        "categories": ["Uncategorized"],
        "tags": ["auto-stub"],
        "created": today,
        "updated": today,
        "sources": [],
    }
    body = (
        f"# {target.replace('_', ' ')}\n\n"
        "> This is an auto-generated stub page. It was referenced by another wiki page but did not exist.\n"
        "> Please expand with real content when information becomes available.\n"
    )
    
    try:
        path = os.path.join(wiki_dir, f"{target}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write("---\n")
            dump_yaml(frontmatter, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
            f.write("---\n")
            f.write(body)
        stubs += 1
        existing_files.add(target)
        print(f"Created stub: {target}.md")
    except Exception as e:
        print(f"Failed to create stub {target}.md: {e}")

print(f"Created {stubs} stub pages.")
