import os
import re
import math
import subprocess
import sys

# Add vector_lake plugin path
plugin_dir = os.path.expanduser(r"~\.gemini\config\plugins\vector-lake")
sys.path.append(plugin_dir)

from vector_lake.governance_store import load_governance_queue

SHARD_SIZE = 50
TMP_DIR = os.path.join(plugin_dir, "tmp", "janitor_shards")

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

    processes = []
    
    for i in range(num_shards):
        shard_items = merge_items[i * SHARD_SIZE : (i + 1) * SHARD_SIZE]
        shard_file = os.path.join(TMP_DIR, f"shard_{i+1}.md")
        
        with open(shard_file, "w", encoding="utf-8") as f:
            f.write(f"# Vector Lake Janitor Swarm - Shard {i+1}\n\n")
            f.write("## DIRECTIVE\n")
            f.write("You are an autonomous Janitor Subagent. Your task is to process the following governance items and merge duplicate entities.\n")
            f.write("For each item, use `resolve_governance_item` MCP tool with resolution 'merge', or use `bulk_reconciliation` MCP tool to physically merge them.\n")
            f.write("CRITICAL for bulk_reconciliation: The payload MUST be a JSON string, NOT a Python list. Ensure each operation uses 'source_entity' and 'target_entity' keys (not 'source_node').\n")
            f.write("CRITICAL: You are running in autonomous background mode. Do NOT ask the user for permission. Execute all merges directly.\n\n")
            f.write("## Governance Items to Process:\n")
            for item in shard_items:
                f.write(f"- Item ID: {item.get('item_id', 'Unknown')}\n")
                f.write(f"  Title: {item.get('title', '')}\n")
                f.write(f"  Description: {item.get('description', '')}\n\n")

        print(f"Launching Swarm Janitor Subagent for Shard {i+1}...")
        
        prompt = f"Please read the instructions in {shard_file} and execute the cleanup. Do not stop until the entire shard is processed."
        
        try:
            p = subprocess.Popen(
                ["agy", "-p", prompt],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=os.environ.copy()
            )
            processes.append(p)
        except FileNotFoundError:
            print("Error: 'agy' CLI command not found. Ensure Antigravity is in PATH.")
            return

    print(f"\n[Success] {num_shards} Janitor Subagents launched in the background!")

if __name__ == "__main__":
    main()
