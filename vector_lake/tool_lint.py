import datetime
import logging
import os
import random
import re
import string
from collections import defaultdict

from vector_lake import governance_metrics
from vector_lake import governance_store
from vector_lake.merge_analysis import filename_candidate_pairs, normalize_name
from vector_lake.wiki_utils import (
    get_wiki_dir,
    iter_wiki_link_matches,
    read_markdown_file,
    write_markdown_file,
)
from vector_lake.schema_validator import SchemaViolationException, VALID_STATUS, validate_schema


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-lint")

_TEMPORAL_LINK = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _register_link_target(
    exact_map: dict[str, str],
    normalized_map: dict[str, set[str]],
    label: str,
    node_key: str,
) -> None:
    cleaned = str(label or "").strip()
    if not cleaned:
        return
    exact_map[cleaned] = node_key
    normalized = normalize_name(cleaned)
    if normalized:
        normalized_map[normalized].add(node_key)


def _resolve_link_target(
    target: str,
    exact_map: dict[str, str],
    normalized_map: dict[str, set[str]],
) -> str | None:
    cleaned = str(target or "").strip()
    if not cleaned or _TEMPORAL_LINK.fullmatch(cleaned):
        return None
    exact = exact_map.get(cleaned)
    if exact:
        return exact
    matches = normalized_map.get(normalize_name(cleaned), set())
    if len(matches) == 1:
        return next(iter(matches))
    return None


def _write_fixed_frontmatter(filepath: str, frontmatter: dict, body: str):
    try:
        write_markdown_file(filepath, frontmatter, body, skip_validation=False)
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
    valid_types = {"vendor", "institution", "product", "person", "event", "concept", "policy", "standard", "source", "synthesis", "system"}
    valid_status = {status.lower() for status in VALID_STATUS}
    valid_epistemic = {"seed", "sprouting", "evergreen"}
    valid_categories = {
        "Uncategorized", "Artificial_Intelligence", "Healthcare_IT",
        "Strategy_and_Business", "System_Architecture",
        "Philosophy_and_Cognitive", "Biomedicine",
        "Policy_and_Governance", "Entities_and_Actors",
    }
    valid_prefixes = ("Concept_", "Vendor_", "Institution_", "Product_", "Person_", "Event_", "Policy_", "Standard_", "Source_", "Synthesis_", "System_")
    required_fields = ["title", "type", "domain", "status", "epistemic-status", "categories"]

    files = [name for name in os.listdir(wiki_dir) if name.endswith(".md") and name not in skip_files]
    issues = {key: [] for key in ["frontmatter", "schema", "naming", "type_status", "category", "duplicate_id", "alias_conflict", "broken_links", "orphan", "reviewed_orphan", "similarity", "decay", "semantic_gc", "governance", "managed_governance", "alignment"]}
    fixes_applied = 0

    parsed = {}
    id_map = {}
    alias_map = {}
    all_keys = set()
    link_target_map = {}
    normalized_link_target_map = defaultdict(set)
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

        links = {
            match.group(1).strip().replace(".md", "")
            for match in iter_wiki_link_matches(content)
        }
        links.discard("")

        parsed[filename] = {"fm": frontmatter, "body": body, "links": links, "path": filepath}

        node_id = frontmatter.get("id", "")
        if node_id:
            id_map.setdefault(str(node_id), []).append(filename)

        _register_link_target(
            link_target_map,
            normalized_link_target_map,
            node_key,
            node_key,
        )
        title = frontmatter.get("title")
        if title:
            _register_link_target(
                link_target_map,
                normalized_link_target_map,
                str(title),
                node_key,
            )

        aliases = frontmatter.get("aliases", [])
        if isinstance(aliases, str):
            aliases = [aliases]
        if isinstance(aliases, list):
            for alias in aliases:
                alias_str = str(alias).strip()
                _register_link_target(
                    link_target_map,
                    normalized_link_target_map,
                    alias_str,
                    node_key,
                )
                alias_map.setdefault(alias_str, []).append(filename)

    for filename, data in parsed.items():
        for target in data["links"]:
            real_key = _resolve_link_target(
                target,
                link_target_map,
                normalized_link_target_map,
            )
            if real_key:
                inbound_count[real_key] += 1

    # Apply Auto-fixes iteratively
    # 1. Naming Compliance
    renamed_files = {}
    for filename in files:
        if not filename.startswith(valid_prefixes):
            issues["naming"].append(f"{filename}: Does not start with valid prefix")
            if auto_fix:
                new_filename = f"Concept_{filename}"
                from vector_lake.wiki_utils import normalize_entity_name
                normalized_new = normalize_entity_name(new_filename[:-3]) + ".md"
                
                from vector_lake.tool_rename import rename_vector_lake_entity
                result = rename_vector_lake_entity(filename, normalized_new, dry_run=False)
                if "Error" in result or "failed" in result.lower():
                    log.error(f"Auto-fix rename failed for {filename}: {result}")
                    continue
                
                renamed_files[filename] = normalized_new
                all_keys.remove(filename[:-3])
                all_keys.add(normalized_new[:-3])
                fixes_applied += 1

    # Update parsed dict if renaming occurred
    if renamed_files:
        new_parsed = {}
        for fname, data in parsed.items():
            if fname in renamed_files:
                new_fname = renamed_files[fname]
                old_key = fname[:-3]
                new_key = new_fname[:-3]
                data["path"] = os.path.join(wiki_dir, new_fname)
                new_parsed[new_fname] = data
                
                for t, rk in list(link_target_map.items()):
                    if rk == old_key:
                        link_target_map[t] = new_key
                if old_key in inbound_count:
                    inbound_count[new_key] += inbound_count.pop(old_key)
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
                        if isinstance(aliases, str):
                            aliases = [aliases]
                        if alias in aliases:
                            aliases.remove(alias)
                            parsed[fname]["fm"]["aliases"] = aliases
                            _write_fixed_frontmatter(parsed[fname]["path"], parsed[fname]["fm"], parsed[fname]["body"])
                            fixes_applied += 1

    # 4. Broken Links. Missing targets require explicit governance; creating
    # empty stubs would turn a topology error into unsupported fake knowledge.
    for filename, data in parsed.items():
        for target in data["links"]:
            if _TEMPORAL_LINK.fullmatch(target):
                continue
            resolved_target = _resolve_link_target(
                target,
                link_target_map,
                normalized_link_target_map,
            )
            if not resolved_target:
                issues["broken_links"].append(f"{filename} -> [[{target}]]: target does not exist")

    # 5. Frontmatter, Type, Status, Category
    for filename, data in parsed.items():
        frontmatter = data["fm"]
        changed = False

        missing = [field for field in required_fields if not frontmatter.get(field)]
        if missing:
            issues["frontmatter"].append(f"{filename}: Missing fields: {', '.join(missing)}")
            if auto_fix:
                if not frontmatter.get("id"):
                    frontmatter["id"] = _generate_id()
                if not frontmatter.get("title"):
                    frontmatter["title"] = filename[:-3]
                if not frontmatter.get("type"):
                    frontmatter["type"] = filename.split("_", 1)[0].lower()
                if not frontmatter.get("domain"):
                    frontmatter["domain"] = "General"
                if not frontmatter.get("topic_cluster"):
                    frontmatter["topic_cluster"] = "General"
                if not frontmatter.get("status"):
                    frontmatter["status"] = "Active"
                if not frontmatter.get("epistemic-status"):
                    frontmatter["epistemic-status"] = "seed"
                if not frontmatter.get("categories"):
                    frontmatter["categories"] = ["Uncategorized"]
                if not frontmatter.get("updated"):
                    frontmatter["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                if "sources" not in frontmatter:
                    frontmatter["sources"] = []
                if not frontmatter.get("strategic_scope"):
                    frontmatter["strategic_scope"] = "edge"
                if not frontmatter.get("evidence_tier"):
                    frontmatter["evidence_tier"] = "derived"
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
                frontmatter["status"] = "Active"
                changed = True

        epistemic = str(frontmatter.get("epistemic-status", "")).lower()
        if epistemic and epistemic not in valid_epistemic:
            issues["type_status"].append(f"{filename}: Invalid epistemic-status '{epistemic}'")
            if auto_fix:
                frontmatter["epistemic-status"] = "seed"
                changed = True

        categories = frontmatter.get("categories", [])
        if isinstance(categories, str):
            categories = [categories]
        if isinstance(categories, list):
            new_cats = []
            for category in categories:
                if category not in valid_categories:
                    issues["category"].append(f"{filename}: Invalid category '{category}'")
                    if auto_fix:
                        changed = True
                        if "Uncategorized" not in new_cats:
                            new_cats.append("Uncategorized")
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

        try:
            validate_schema(frontmatter, data["body"], filename)
        except SchemaViolationException as e:
            issues["schema"].append(f"{filename}: {str(e)}")

    # 6. Filename similarity candidates. Actual merge decisions use governance analysis.
    for key_a, key_b, ratio in filename_candidate_pairs(all_keys):
        issues["similarity"].append(
            f"Candidate: {key_a}.md <-> {key_b}.md ({ratio:.0%})"
        )

    # Remaining checks (Orphans, Decay, Governance, Alignment)
    for filename in files:
        node_key = filename[:-3]
        if inbound_count.get(node_key, 0) == 0 and not filename.startswith("Source_"):
            topology_status = str(
                parsed.get(filename, {}).get("fm", {}).get("topology_status", "")
            ).strip().lower()
            review_due = str(
                parsed.get(filename, {}).get("fm", {}).get("topology_review_due", "")
            ).strip()
            review_owner = str(
                parsed.get(filename, {}).get("fm", {}).get("topology_review_owner", "")
            ).strip()
            review_basis = str(
                parsed.get(filename, {}).get("fm", {}).get("topology_review_basis", "")
            ).strip()
            try:
                due_is_current = (
                    datetime.date.fromisoformat(review_due)
                    >= datetime.datetime.now(datetime.timezone.utc).date()
                )
            except ValueError:
                due_is_current = False
            if (
                topology_status == "acknowledged-orphan"
                and review_owner
                and review_basis == "no-resolvable-inbound-links"
                and due_is_current
            ):
                issues["reviewed_orphan"].append(
                    f"{filename}: Acknowledged orphan; owner={review_owner}; due={review_due}"
                )
            else:
                issues["orphan"].append(f"{filename}: No inbound links (orphan)")

    DEFAULT_TTL = {
        "source": 365,
        "synthesis": 730,
        "vendor": 1095,
        "product": 1095,
        "person": 1095,
        "event": 1095,
        "policy": 1095,
        "standard": 1095,
        "concept": 1825,
    }

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    for filename, data in parsed.items():
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
            if node_status == "active":
                issues["alignment"].append(f"{filename}: Alignment Score {alignment_score} < 60 while status is Active; review for Draft, Superseded, or Deprecated.")

    # 7. Semantic Garbage Collection (Auto-GC)
    archive_dir = os.path.join(wiki_dir, ".archive")
    import shutil
    for filename, data in parsed.items():
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
                
        is_contested = node_status in ["superseded", "deprecated"]
        has_low_inbound = inbound_count.get(node_key, 0) <= 1
        is_empty = len(data["body"].strip()) < 50 and not filename.startswith("Source_")
        
        if (is_contested and age_days > 30 and has_low_inbound) or (is_empty and age_days > 30 and has_low_inbound):
            issues["semantic_gc"].append(f"{filename}: GC triggered (Status: {node_status}, Age: {age_days}d, Inbound: {inbound_count.get(node_key, 0)})")
            if auto_fix:
                if not os.path.exists(archive_dir):
                    os.makedirs(archive_dir)
                try:
                    archive_path = os.path.join(archive_dir, filename)
                    shutil.copy2(data["path"], archive_path)
                    from vector_lake.mutation_coordinator import execute_mutation_plan
                    execute_mutation_plan(filename, is_delete=True)
                    fixes_applied += 1
                    log.info(f"[Semantic GC] Archived stale node: {filename}")
                except Exception as e:
                    log.error(f"Failed to archive {filename}: {e}")

    governance_store.initialize_meta_store()
    metrics = governance_metrics.compute_debt_metrics()
    if metrics["unmanaged_unsupported_claim_count"] > 0:
        issues["governance"].append(
            f"Unmanaged unsupported claims: {metrics['unmanaged_unsupported_claim_count']}"
        )
    if metrics["managed_unsupported_claim_count"] > 0:
        issues["managed_governance"].append(
            f"Acknowledged evidence-gap claims (owner/due/version bound): {metrics['managed_unsupported_claim_count']}"
        )
    if metrics["unmanaged_missing_link_target_count"] > 0:
        issues["governance"].append(
            f"Unmanaged missing-link targets: {metrics['unmanaged_missing_link_target_count']}"
        )
    if metrics["managed_missing_link_target_count"] > 0:
        issues["managed_governance"].append(
            "Acknowledged missing-link targets (owner/due bound): "
            f"{metrics['managed_missing_link_target_count']}"
        )
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
        "reviewed_orphan": "8b. Acknowledged Orphan Debt",
        "similarity": "9. Filename Similarity",
        "decay": "10. Knowledge Decay",
        "semantic_gc": "11. Semantic Garbage Collection",
        "governance": "12. Governance Debt",
        "managed_governance": "12b. Managed Governance Debt",
        "alignment": "13. Alignment Drift",
        "schema": "14. Strict Schema Verification",
    }

    informational_checks = {"similarity", "reviewed_orphan", "managed_governance"}
    total_issues = sum(
        len(items)
        for key, items in issues.items()
        if key not in informational_checks
    )
    managed_debt_count = (
        len(issues["reviewed_orphan"])
        + int(metrics["managed_unsupported_claim_count"])
        + int(metrics["managed_missing_link_target_count"])
    )
    lines = [
        "=== Vector Lake Lint Report ===",
        f"Scanned: {len(files)} files | Issues: {total_issues} | Managed debt: {managed_debt_count} | Auto-fixed: {fixes_applied}",
        "",
    ]
    for key, name in check_names.items():
        items = issues[key]
        if key in informational_checks:
            state = "[PASS]" if not items else f"[INFO: {len(items)}]"
        else:
            state = "[PASS]" if not items else f"[FAIL: {len(items)}]"
        lines.append(f"{name}: {state}")
        for item in items[:10]:
            lines.append(f"    {item}")
        if len(items) > 10:
            lines.append(f"    ... and {len(items) - 10} more")
        lines.append("")
    return "\n".join(lines)
