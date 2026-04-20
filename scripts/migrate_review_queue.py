import json
import logging
import os
import uuid
from pathlib import Path

from vector_lake.wiki_utils import get_review_queue_path
from vector_lake.governance_store import load_governance_queue, save_governance_queue

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-migration")

def main():
    old_queue_path = get_review_queue_path()
    if not old_queue_path.exists():
        log.info("No legacy review_queue.json found. Nothing to migrate.")
        return

    try:
        with open(old_queue_path, "r", encoding="utf-8") as f:
            old_items = json.load(f)
    except Exception as e:
        log.error(f"Failed to read legacy queue: {e}")
        return

    if not old_items:
        log.info("Legacy queue is empty. Migrating file only.")
        os.rename(old_queue_path, str(old_queue_path) + ".migrated")
        return

    gov_queue = load_governance_queue()
    existing_pending = {
        item.get("fingerprint") for item in gov_queue["items"]
        if item.get("status") == "pending" and item.get("fingerprint")
    }

    migrated_count = 0
    for item in old_items:
        if item.get("status") != "pending":
            continue

        fingerprint = item.get("fingerprint", "")
        if fingerprint and fingerprint in existing_pending:
            continue

        gov_item = dict(item)
        if not gov_item.get("item_id"):
            gov_item["item_id"] = f"gov_{uuid.uuid4().hex[:12]}"
        
        gov_queue["items"].append(gov_item)
        if fingerprint:
            existing_pending.add(fingerprint)
        migrated_count += 1

    save_governance_queue(gov_queue)
    os.rename(old_queue_path, str(old_queue_path) + ".migrated")
    log.info(f"Successfully migrated {migrated_count} pending items to governance queue.")

if __name__ == "__main__":
    main()
