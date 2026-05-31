import datetime
import logging
import os
import random
import re
import string
from collections import defaultdict
from difflib import SequenceMatcher

from vector_lake import governance_metrics
from vector_lake import governance_store
from vector_lake.wiki_utils import get_wiki_dir, read_markdown_file, write_markdown_file


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-lint")


def _write_fixed_frontmatter(filepath: str, frontmatter: dict, body: str):
    try:
        write_markdown_file(filepath, frontmatter, body)
    except Exception as e:
        log.warning(f"Failed to write fixed frontmatter to {filepath}: {e}")

def _generate_id():
    today = datetime.datetime.now().strftime("%Y%m%d")
    return f"{today}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=6))}"

def lint_vector_lake(auto_fix: bool = False):
    wiki_dir = str(get_wiki_dir())
    if not os.path.exists(wiki_dir):
        return "Wiki directory not found."

    skip_files = {"index.md", "log.md", "overview.md"}
    valid_types = {"vendor", "product", "person", "event", "concept", "source", "synthesis"}
    valid_status = {"active", "deprecated", "archived", "contested"}
    valid_epistemic = {"seed", "sprouting", "evergreen"}
    valid_categories = {
        "Uncategorized", "Artificial_Intelligence", "Healthcare_IT",
        "Strategy_and_Business", "System_Architecture",
        "Philosophy_and_Cognitive", "Biomedicine",
    }
    valid_prefixes = ("Concept_", "Vendor_", "Product_", "Person_", "Event_", "Source_", "Synthesis_")
    required_fields = ["title", "type", "domain", "status", "epistemic-status", "categories"]

    files = [name for name in os.listdir(wiki_dir) if name.endswith(".md") and name not in skip_files]
    issues = {key: [] for key in ["frontmatter", "naming", "type_status", "category", "duplicate_id", "alias_conflict", "broken_links", "orphan", "similarity", "decay", "semantic_gc", "governance", "alignment"]}
    fixes_applied = 0

    parsed = {}
    id_map = {}
    alias_map = {}
    all_keys = set()
    inbound_count = defaultdict(int)

    # First Pass: Read and parse
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
        for match in re.finditer(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", content):
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

    # Apply Auto-fixes iteratively
    # 1. Naming Compliance
    renamed_files = {}
    for filename in files:
        if not filename.startswith(valid_prefixes):
            issues["naming"].append(f"{filename}: Does not start with valid prefix")
            if auto_fix:
                new_filename = f"Concept_{filename}"
                old_path = os.path.join(wiki_dir, filename)
                new_path = os.path.join(wiki_dir, new_filename)
                try:
                    os.replace(old_path, new_path)
                except Exception as e:
                    log.error(f"Failed to rename {old_path} to {new_path}: {e}")
                    continue
                renamed_files[filename] = new_filename
                all_keys.remove(filename[:-3])
                all_keys.add(new_filename[:-3])
                fixes_applied += 1

    # Update parsed dict if renaming occurred
    if renamed_files:
        new_parsed = {}
        for fname, data in parsed.items():
            if fname in renamed_files:
                new_fname = renamed_files[fname]
                data["path"] = os.path.join(wiki_dir, new_fname)
                new_parsed[new_fname] = data
            else:
                new_parsed[fname] = data
        parsed = new_parsed
        files = list(parsed.keys())

    # 2. Duplicate IDs
    for node_id, filenames in id_map.items():
        if len(filenames) > 1:
            issues["duplicate_id"].append(f"ID '{node_id}' shared by: {', '.join(filenames)}")
            if auto_fix:
                for fname in filenames[1:]:
                    if fname in parsed:
                        parsed[fname]["fm"]["id"] = _generate_id()
                        _write_fixed_frontmatter(parsed[fname]["path"], parsed[fname]["fm"], parsed[fname]["body"])
                        fixes_applied += 1

    # 3. Alias Conflicts
    for alias, filenames in alias_map.items():
        if len(filenames) > 1:
            issues["alias_conflict"].append(f"Alias '{alias}' claimed by: {', '.join(filenames)}")
            if auto_fix:
                for fname in filenames[1:]:
                    if fname in parsed:
                        aliases = parsed[fname]["fm"].get("aliases", [])
                        if isinstance(aliases, str): aliases = [aliases]
                        if alias in aliases:
                            aliases.remove(alias)
                            parsed[fname]["fm"]["aliases"] = aliases
                            _write_fixed_frontmatter(parsed[fname]["path"], parsed[fname]["fm"], parsed[fname]["body"])
                            fixes_applied += 1

    # 4. Broken Links (Stub Creation)
    for filename, data in parsed.items():
        for target in data["links"]:
            if target not in all_keys:
                issues["broken_links"].append(f"{filename} -> [[{target}]]: target does not exist")
                if auto_fix:
                    stub_filename = f"Concept_{target}.md" if not target.startswith(valid_prefixes) else f"{target}.md"
                    stub_filename = re.sub(r'[\\/*?:"<>|]', "_", stub_filename)
                    stub_path = os.path.join(wiki_dir, stub_filename)
                    if not os.path.exists(stub_path):
                        stub_fm = {
                            "id": _generate_id(),
                            "title": target,
                            "type": "concept",
                            "domain": "General",
                            "status": "active",
                            "epistemic-status": "seed",
                            "categories": ["Uncategorized"],
                            "created": datetime.datetime.now().strftime("%Y-%m-%d"),
                            "updated": datetime.datetime.now().strftime("%Y-%m-%d")
                        }
                        _write_fixed_frontmatter(stub_path, stub_fm, f"\n# {target}\n\nThis is an auto-generated stub page to prevent broken links from [[{filename[:-3]}]].\n")
                        all_keys.add(stub_filename[:-3])
                        fixes_applied += 1

    # 5. Frontmatter, Type, Status, Category
    for filename, data in parsed.items():
        frontmatter = data["fm"]
        changed = False

        missing = [field for field in required_fields if not frontmatter.get(field)]
        if missing:
            issues["frontmatter"].append(f"{filename}: Missing fields: {', '.join(missing)}")
            if auto_fix:
                if not frontmatter.get("title"): frontmatter["title"] = filename[:-3]
                if not frontmatter.get("domain"): frontmatter["domain"] = "General"
                if not frontmatter.get("topic_cluster"): frontmatter["topic_cluster"] = "General"
                if not frontmatter.get("status"): frontmatter["status"] = "active"
                if not frontmatter.get("epistemic-status"): frontmatter["epistemic-status"] = "seed"
                if not frontmatter.get("categories"): frontmatter["categories"] = ["Uncategorized"]
                changed = True

        file_type = str(frontmatter.get("type", "")).lower()
        if file_type and file_type not in valid_types:
            issues["type_status"].append(f"{filename}: Invalid type '{file_type}'")
            if auto_fix:
                frontmatter["type"] = "concept"
                changed = True

        status = str(frontmatter.get("status", "")).lower()
        if status and status not in valid_status:
            issues["type_status"].append(f"{filename}: Invalid status '{status}'")
            if auto_fix:
                frontmatter["status"] = "active"
                changed = True

        epistemic = str(frontmatter.get("epistemic-status", "")).lower()
        if epistemic and epistemic not in valid_epistemic:
            issues["type_status"].append(f"{filename}: Invalid epistemic-status '{epistemic}'")
            if auto_fix:
                frontmatter["epistemic-status"] = "seed"
                changed = True

        categories = frontmatter.get("categories", [])
        if isinstance(categories, str): categories = [categories]
        if isinstance(categories, list):
            new_cats = []
            for category in categories:
                if category not in valid_categories:
                    issues["category"].append(f"{filename}: Invalid category '{category}'")
                    if auto_fix:
                        changed = True
                        if "Uncategorized" not in new_cats: new_cats.append("Uncategorized")
                else:
                    new_cats.append(category)
            if auto_fix and not new_cats:
                new_cats.append("Uncategorized")
                changed = True
            if auto_fix and changed:
                frontmatter["categories"] = new_cats

        if auto_fix and changed:
            _write_fixed_frontmatter(data["path"], frontmatter, data["body"])
            fixes_applied += 1

    # 6. Similarity Merge (>0.91)
    keys_list = sorted(list(all_keys))
    merged_keys = set()
    for index, key_a in enumerate(keys_list):
        if key_a in merged_keys: continue
        for other_index in range(index + 1, min(index + 50, len(keys_list))):
            key_b = keys_list[other_index]
            if key_b in merged_keys: continue
            
            prefix_a = key_a.split("_")[0] if "_" in key_a else ""
            prefix_b = key_b.split("_")[0] if "_" in key_b else ""
            if prefix_a != prefix_b:
                continue
            name_a = key_a.split("_", 1)[1] if "_" in key_a else key_a
            name_b = key_b.split("_", 1)[1] if "_" in key_b else key_b
            ratio = SequenceMatcher(None, name_a.lower(), name_b.lower()).ratio()
            
            if ratio > 0.91 and key_a != key_b:
                issues["similarity"].append(f"Duplicate: {key_a}.md <-> {key_b}.md ({ratio:.0%})")
                if False: # auto_fix disabled for similarity merge by Mentat
                    # Determine Primary vs Secondary based on 'updated' date
                    file_a = f"{key_a}.md"
                    file_b = f"{key_b}.md"
                    if file_a not in parsed or file_b not in parsed: continue
                    
                    fm_a = parsed[file_a]["fm"]
                    fm_b = parsed[file_b]["fm"]
                    date_a = fm_a.get("updated", "")
                    date_b = fm_b.get("updated", "")
                    
                    if date_b > date_a:
                        primary, secondary = file_b, file_a
                        p_key, s_key = key_b, key_a
                    else:
                        primary, secondary = file_a, file_b
                        p_key, s_key = key_a, key_b
                    
                    p_data = parsed[primary]
                    s_data = parsed[secondary]
                    
                    # Append Body
                    new_body = p_data["body"] + f"\n\n---\n## Auto-Merged from {s_key}\n\n" + s_data["body"]
                    p_data["body"] = new_body
                    
                    # Append Alias
                    p_aliases = p_data["fm"].get("aliases", [])
                    if isinstance(p_aliases, str): p_aliases = [p_aliases]
                    if s_key not in p_aliases: p_aliases.append(s_key)
                    s_aliases = s_data["fm"].get("aliases", [])
                    if isinstance(s_aliases, str): s_aliases = [s_aliases]
                    for alias in s_aliases:
                        if alias not in p_aliases: p_aliases.append(alias)
                    p_data["fm"]["aliases"] = p_aliases
                    p_data["fm"]["updated"] = datetime.datetime.now().strftime("%Y-%m-%d")
                    
                    # Write Primary
                    _write_fixed_frontmatter(p_data["path"], p_data["fm"], p_data["body"])
                    
                    # Delete Secondary
                    try:
                        os.remove(s_data["path"])
                    except Exception as e:
                        log.error(f"Failed to delete merged secondary {s_data['path']}: {e}")
                    
                    merged_keys.add(s_key)
                    fixes_applied += 1

    # Remaining checks (Orphans, Decay, Governance, Alignment)
    for filename in files:
        if filename[:-3] in merged_keys: continue
        node_key = filename[:-3]
        if inbound_count.get(node_key, 0) == 0 and not filename.startswith("Source_"):
            issues["orphan"].append(f"{filename}: No inbound links (orphan)")

    DEFAULT_TTL = {
        "source": 365,
        "synthesis": 730,
        "vendor": 1095,
        "product": 1095,
        "person": 1095,
        "event": 1095,
        "concept": 1825,
    }

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    for filename, data in parsed.items():
        if filename[:-3] in merged_keys: continue
        frontmatter = data["fm"]
        updated_str = str(frontmatter.get("updated", ""))
        if not updated_str:
            continue
        try:
            updated_dt = datetime.datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
            if updated_dt.tzinfo is None:
                updated_dt = updated_dt.replace(tzinfo=datetime.timezone.utc)
            age_days = (now_utc - updated_dt).days
        except (ValueError, TypeError):
            continue
            
        if age_days > 0:
            node_type = str(frontmatter.get("type", "concept")).lower().strip()
            ttl = frontmatter.get("ttl")
            if not isinstance(ttl, (int, float)):
                ttl = DEFAULT_TTL.get(node_type, 1095)
            
            if ttl > 0:
                decay_weight = 0.5 ** (age_days / ttl)
                if decay_weight < 0.2:
                    issues["decay"].append(f"{filename}: Severe Knowledge Decay (weight: {decay_weight:.2f}, age: {age_days}d, ttl: {ttl})")

        alignment_score = frontmatter.get("alignment_score")
        if isinstance(alignment_score, (int, float)) and alignment_score < 60:
            node_status = str(frontmatter.get("status", "")).lower()
            if node_status not in ["contested", "misaligned"]:
                issues["alignment"].append(f"{filename}: Alignment Score {alignment_score} < 60 but status is '{node_status}', MUST be 'Contested'")

    # 7. Semantic Garbage Collection (Auto-GC)
    archive_dir = os.path.join(wiki_dir, ".archive")
    import shutil
    for filename, data in parsed.items():
        if filename[:-3] in merged_keys: continue
        frontmatter = data["fm"]
        node_status = str(frontmatter.get("status", "")).lower()
        node_key = filename[:-3]
        
        updated_str = str(frontmatter.get("updated", ""))
        age_days = 0
        if updated_str:
            try:
                updated_dt = datetime.datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
                if updated_dt.tzinfo is None:
                    updated_dt = updated_dt.replace(tzinfo=datetime.timezone.utc)
                age_days = (now_utc - updated_dt).days
            except (ValueError, TypeError):
                pass
                
        is_contested = node_status in ["contested", "unsupported", "misaligned"]
        has_low_inbound = inbound_count.get(node_key, 0) <= 1
        is_empty = len(data["body"].strip()) < 50 and not filename.startswith("Source_")
        
        if (is_contested and age_days > 30 and has_low_inbound) or (is_empty and age_days > 30 and has_low_inbound):
            issues["semantic_gc"].append(f"{filename}: GC triggered (Status: {node_status}, Age: {age_days}d, Inbound: {inbound_count.get(node_key, 0)})")
            if auto_fix:
                if not os.path.exists(archive_dir):
                    os.makedirs(archive_dir)
                try:
                    shutil.move(data["path"], os.path.join(archive_dir, filename))
                    fixes_applied += 1
                    log.info(f"[Semantic GC] Archived stale node: {filename}")
                except Exception as e:
                    log.error(f"Failed to archive {filename}: {e}")

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
        "semantic_gc": "11. Semantic Garbage Collection",
        "governance": "12. Governance Debt",
        "alignment": "13. Alignment Drift",
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
