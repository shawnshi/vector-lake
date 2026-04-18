import datetime
import logging
import os
import re
from collections import defaultdict
from difflib import SequenceMatcher

import governance_metrics
import governance_store
from wiki_utils import get_wiki_dir, read_markdown_file, write_markdown_file


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-lint")


def _write_fixed_frontmatter(filepath: str, frontmatter: dict, body: str):
    try:
        write_markdown_file(filepath, frontmatter, body)
    except Exception as e:
        log.warning(f"Failed to write fixed frontmatter to {filepath}: {e}")


def lint_vector_lake(auto_fix: bool = False):
    wiki_dir = str(get_wiki_dir())
    if not os.path.exists(wiki_dir):
        return "Wiki directory not found."

    skip_files = {"index.md", "log.md", "overview.md"}
    valid_types = {"entity", "concept", "source", "synthesis"}
    valid_status = {"active", "deprecated", "archived", "contested"}
    valid_epistemic = {"seed", "sprouting", "evergreen"}
    valid_categories = {
        "Uncategorized", "Artificial_Intelligence", "Healthcare_IT",
        "Strategy_and_Business", "System_Architecture",
        "Philosophy_and_Cognitive", "Biomedicine",
    }
    valid_prefixes = ("Concept_", "Source_", "Entity_", "Synthesis_", "Event_", "Person_", "Project_", "Term_", "System_")
    required_fields = ["title", "type", "domain", "status", "epistemic-status", "categories"]

    files = [name for name in os.listdir(wiki_dir) if name.endswith(".md") and name not in skip_files]
    issues = {key: [] for key in ["frontmatter", "naming", "type_status", "category", "duplicate_id", "alias_conflict", "broken_links", "orphan", "similarity", "decay", "governance"]}
    fixes_applied = 0

    parsed = {}
    id_map = {}
    alias_map = {}
    all_keys = set()
    inbound_count = defaultdict(int)

    for filename in files:
        filepath = os.path.join(wiki_dir, filename)
        node_key = filename[:-3]
        all_keys.add(node_key)
        try:
            frontmatter, body, content = read_markdown_file(filepath)
        except Exception:
            issues["frontmatter"].append(f"{filename}: Cannot read file")
            continue

        if not content.startswith("---"):
            issues["frontmatter"].append(f"{filename}: Missing YAML frontmatter entirely")
            continue

        links = set()
        for match in re.finditer(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]", content):
            links.add(match.group(1).strip().replace(".md", ""))
        for match in re.finditer(r"\[[^\[\]]+?::\s*\[\[([^\]]+?)\]\]\]", content):
            links.add(match.group(1).strip().split("|")[0].strip().replace(".md", ""))
        links.discard("")

        parsed[filename] = {"fm": frontmatter, "body": body, "links": links, "path": filepath}

        node_id = frontmatter.get("id", "")
        if node_id:
            id_map.setdefault(str(node_id), []).append(filename)

        aliases = frontmatter.get("aliases", [])
        if isinstance(aliases, str):
            aliases = [aliases]
        if isinstance(aliases, list):
            for alias in aliases:
                alias_map.setdefault(str(alias).strip(), []).append(filename)

        for target in links:
            inbound_count[target] += 1

    for filename, data in parsed.items():
        frontmatter = data["fm"]
        missing = [field for field in required_fields if not frontmatter.get(field)]
        if missing:
            issues["frontmatter"].append(f"{filename}: Missing fields: {', '.join(missing)}")
            if auto_fix:
                changed = False
                if not frontmatter.get("domain"):
                    frontmatter["domain"] = "General"
                    changed = True
                if not frontmatter.get("topic_cluster"):
                    frontmatter["topic_cluster"] = "General"
                    changed = True
                if not frontmatter.get("status"):
                    frontmatter["status"] = "Active"
                    changed = True
                if not frontmatter.get("epistemic-status"):
                    frontmatter["epistemic-status"] = "seed"
                    changed = True
                if not frontmatter.get("categories"):
                    frontmatter["categories"] = ["Uncategorized"]
                    changed = True
                if changed:
                    _write_fixed_frontmatter(data["path"], frontmatter, data["body"])
                    fixes_applied += 1

    for filename in files:
        if not filename.startswith(valid_prefixes):
            issues["naming"].append(f"{filename}: Does not start with valid prefix ({'/'.join(valid_prefixes)})")

    for filename, data in parsed.items():
        frontmatter = data["fm"]
        file_type = str(frontmatter.get("type", "")).lower()
        if file_type and file_type not in valid_types:
            issues["type_status"].append(f"{filename}: Invalid type '{file_type}' (valid: {valid_types})")

        status = str(frontmatter.get("status", "")).lower()
        if status and status not in valid_status:
            issues["type_status"].append(f"{filename}: Invalid status '{status}' (valid: {valid_status})")

        epistemic = str(frontmatter.get("epistemic-status", "")).lower()
        if epistemic and epistemic not in valid_epistemic:
            issues["type_status"].append(f"{filename}: Invalid epistemic-status '{epistemic}' (valid: {valid_epistemic})")

        categories = frontmatter.get("categories", [])
        if isinstance(categories, str):
            categories = [categories]
        if isinstance(categories, list):
            for category in categories:
                if category not in valid_categories:
                    issues["category"].append(f"{filename}: Invalid category '{category}'")

    for node_id, filenames in id_map.items():
        if len(filenames) > 1:
            issues["duplicate_id"].append(f"ID '{node_id}' shared by: {', '.join(filenames)}")

    for alias, filenames in alias_map.items():
        if len(filenames) > 1:
            issues["alias_conflict"].append(f"Alias '{alias}' claimed by: {', '.join(filenames)}")

    for filename, data in parsed.items():
        for target in data["links"]:
            if target not in all_keys:
                issues["broken_links"].append(f"{filename} -> [[{target}]]: target does not exist")

    for filename in files:
        node_key = filename[:-3]
        if inbound_count.get(node_key, 0) == 0 and not filename.startswith("Source_"):
            issues["orphan"].append(f"{filename}: No inbound links (orphan)")

    keys_list = sorted(all_keys)
    for index, key_a in enumerate(keys_list):
        for other_index in range(index + 1, min(index + 50, len(keys_list))):
            key_b = keys_list[other_index]
            prefix_a = key_a.split("_")[0] if "_" in key_a else ""
            prefix_b = key_b.split("_")[0] if "_" in key_b else ""
            if prefix_a != prefix_b:
                continue
            name_a = key_a.split("_", 1)[1] if "_" in key_a else key_a
            name_b = key_b.split("_", 1)[1] if "_" in key_b else key_b
            ratio = SequenceMatcher(None, name_a.lower(), name_b.lower()).ratio()
            if ratio > 0.85 and key_a != key_b:
                issues["similarity"].append(f"Possible duplicate: {key_a}.md <-> {key_b}.md (similarity: {ratio:.0%})")

    today = datetime.datetime.now()
    for filename, data in parsed.items():
        frontmatter = data["fm"]
        if str(frontmatter.get("epistemic-status", "")).lower() != "sprouting":
            continue
        updated_str = str(frontmatter.get("updated", ""))
        if not updated_str:
            continue
        try:
            updated = datetime.datetime.strptime(updated_str.replace("-", "")[:8], "%Y%m%d")
        except (ValueError, TypeError):
            continue
        age_days = (today - updated).days
        if age_days > 60:
            issues["decay"].append(f"{filename}: 'sprouting' for {age_days} days (updated: {updated_str})")
            if auto_fix:
                frontmatter["epistemic-status"] = "evergreen"
                _write_fixed_frontmatter(data["path"], frontmatter, data["body"])
                fixes_applied += 1

    governance_store.initialize_meta_store()
    metrics = governance_metrics.compute_debt_metrics()
    if metrics["unsupported_claim_count"] > 0:
        issues["governance"].append(f"Unsupported claims: {metrics['unsupported_claim_count']}")
    if metrics["stale_claim_count"] > 0:
        issues["governance"].append(f"Stale claims: {metrics['stale_claim_count']}")
    if metrics["pending_change_set_count"] > 0:
        issues["governance"].append(f"Pending change sets: {metrics['pending_change_set_count']}")

    check_names = {
        "frontmatter": "1. Frontmatter Completeness",
        "naming": "2. Naming Compliance",
        "type_status": "3. Type/Status Legality",
        "category": "4. Category Vocabulary",
        "duplicate_id": "5. Duplicate IDs",
        "alias_conflict": "6. Alias Conflicts",
        "broken_links": "7. Broken Links",
        "orphan": "8. Orphan Pages",
        "similarity": "9. Filename Similarity",
        "decay": "10. Knowledge Decay",
        "governance": "11. Governance Debt",
    }

    total_issues = sum(len(items) for items in issues.values())
    lines = ["=== Vector Lake Lint Report ===", f"Scanned: {len(files)} files | Issues: {total_issues} | Auto-fixed: {fixes_applied}", ""]
    for key, name in check_names.items():
        items = issues[key]
        lines.append(f"{name}: {'[PASS]' if not items else f'[FAIL: {len(items)}]'}")
        for item in items[:10]:
            lines.append(f"    {item}")
        if len(items) > 10:
            lines.append(f"    ... and {len(items) - 10} more")
        lines.append("")
    return "\n".join(lines)
