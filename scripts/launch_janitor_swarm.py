import os
import json
import math
import sys
from datetime import datetime, timezone

# Allow `python scripts/launch_janitor_swarm.py` from the repository root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vector_lake import get_extension_root  # noqa: E402
from vector_lake.template_loader import render_template  # noqa: E402
from vector_lake.governance_store import load_governance_queue  # noqa: E402

# Maximum governance items per subagent shard.
SHARD_SIZE = 8
# Shard packets live inside the repository-owned scratch tree, not in a
# machine-specific temp path.
TMP_DIR = str(get_extension_root() / "tmp" / "janitor_swarm")


def main():
    queue = load_governance_queue()
    items = queue.get("items", [])
    
    # Filter for pending merge suggestions or duplicate resolutions
    merge_items = [
        item for item in items 
        if item.get("status") == "pending" and item.get("type") in ("merge_suggestion", "duplicate_alert", "filename_similarity")
    ]

    if not merge_items:
        print("No pending duplicate/merge items found in the governance queue. Janitor Swarm is idle.")
        return

    os.makedirs(TMP_DIR, exist_ok=True)
    num_shards = math.ceil(len(merge_items) / SHARD_SIZE)
    print(f"Found {len(merge_items)} pending merge alerts. Sharding into {num_shards} clusters (Max {SHARD_SIZE} per shard)...")

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": "native-manifest",
        "shard_size": SHARD_SIZE,
        "shards": [],
    }
    
    for i in range(num_shards):
        shard_items = merge_items[i * SHARD_SIZE : (i + 1) * SHARD_SIZE]
        shard_file = os.path.join(TMP_DIR, f"shard_{i+1}.md")
        
        item_text = "".join(render_template(
            "prompts/janitor_item.md", id=item.get("item_id", "Unknown"),
            title=item.get("title", ""), description=item.get("description", ""),
        ) for item in shard_items)
        shard_text = render_template("prompts/janitor.md", shard=i + 1, items=item_text)
        with open(shard_file, "w", encoding="utf-8") as f:
            f.write(shard_text)

        manifest["shards"].append({
            "index": i + 1,
            "path": shard_file,
            "item_ids": [item.get("item_id", "Unknown") for item in shard_items],
        })
        print(f"Prepared native janitor shard {i+1}: {shard_file}")

    manifest_file = os.path.join(TMP_DIR, "janitor_manifest.json")
    with open(manifest_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"\n[OK] Prepared {num_shards} native janitor shard(s). Manifest: {manifest_file}")
    print("No external agent process was launched. Resolve shards through the governance queue or explicit merge tooling.")

if __name__ == "__main__":
    main()
