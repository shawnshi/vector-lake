import datetime
import logging
import os
import re
from collections import defaultdict
from difflib import SequenceMatcher

import yaml

from vector_lake import governance_metrics
from vector_lake import governance_store
from vector_lake.governance_metrics import establishment_key
from vector_lake.semantic_merge import merge_markdown_content
from vector_lake.wiki_utils import (
    VALID_PREFIXES,
    get_wiki_dir,
    normalize_entity_name,
    read_markdown_file,
    split_frontmatter,
    write_markdown_file,
)
from vector_lake import stub_creator
from vector_lake.schema_validator import (
    REQUIRED_FIELDS,
    SYSTEM_ARTIFACT_CATEGORIES,
    VALID_CATEGORIES,
    VALID_EPISTEMIC_STATUS,
    VALID_STATUS,
    VALID_TYPES,
    missing_required_fields,
    validate_schema,
    SchemaViolationException,
)
from vector_lake.node_vocabulary import NON_NODE_WIKI_FILES, strip_prefix


# ``VALID_STATUS`` is capitalised while the check below lowercases the page value,
# so the comparison view is normalised once here instead of restating the set.
_LOWERCASE_STATUS = {status.lower() for status in VALID_STATUS}


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-lint")


def _write_fixed_frontmatter(filepath: str, frontmatter: dict, body: str):
    try:
        write_markdown_file(filepath, frontmatter, body, skip_validation=False)
    except Exception as e:
        log.warning(f"Failed to write fixed frontmatter to {filepath}: {e}")


def _render_page(frontmatter: dict, body: str) -> str:
    """Rebuild the text ``merge_markdown_content`` consumes from a parsed page."""
    rendered = yaml.safe_dump(
        frontmatter, allow_unicode=True, default_flow_style=False, sort_keys=False
    )
    return f"---\n{rendered}---\n{body}"

def _generate_id(stem: str) -> str:
    """The id a page that has none gets.  One owner: :func:`stub_creator.generate_id`.

    This was a second, random generator -- the opposite convention to the one stubs use, for the
    same thing, so two answers existed for "what is a page's id".  Delegating also makes the
    duplicate-id repair idempotent, and two pages sharing an id get different new ones because
    their stems differ.
    """
    return stub_creator.generate_id(stem, datetime.datetime.now().strftime("%Y-%m-%d"))

def resolve_link_target(
    target: str, link_target_map: dict, unique_cores: dict[str, str]
) -> str | None:
    """The page ``target`` names, or ``None`` when no page answers it.

    Two lookups, and the order is the whole design:

    1. ``link_target_map`` as written -- a filename, a title or an alias.  A page declaring a
       name settles it here.
    2. ``unique_cores`` on the target's core name -- ``[[刘宁]]`` for ``Person_刘宁``, or
       ``[[Concept_CoMET]]`` for ``Product_CoMET``, a link written before the page was filed
       under its real type.

    The second lookup is the *unique-cores* map, not ``link_target_map`` with the prefix
    stripped.  Stripping into the general map would also match titles and aliases, and that
    route reaches a page through a name it merely declares -- so a title equal to an ambiguous
    core name would silently resolve a link that two pages already contest.  Measured on the
    live wiki, that mistake resolved 42 further links (788 -> 680 reported broken instead of
    the 721 the rule predicts), which is why this takes the narrower map.

    Both sides are compared *normalised* (``_`` and ``-`` are one name, as everywhere else in
    the wiki).  Without it the closure breaks for a target a stub had to sanitise: ``[[Foo Bar]]``
    produced ``Concept_Foo-Bar.md``, whose core is ``Foo-Bar``, so the raw target matched
    nothing, the link stayed broken on every run, and the stub that had just been written made
    the creator skip it silently -- reported broken forever with nothing that could fix it.  It
    also folds the ``_``/``-`` axis into the uniqueness guard, so ``Concept_Foo-Bar.md`` and
    ``Product_Foo_Bar.md`` count as one contested core instead of two clean ones.
    """
    resolved = link_target_map.get(target)
    if resolved:
        return resolved
    core = normalize_entity_name(strip_prefix(target))
    return unique_cores.get(core)


def core_name_maps(all_keys) -> tuple[dict[str, list[str]], dict[str, str]]:
    """``(core -> pages, unambiguous core -> page)`` for the pages a link may resolve to.

    One function because the two are built together and answer one question between them.  They
    are built once per pass: a rename preserves a page's *core* name, so the maps stay correct
    across the naming auto-fix, and the one stale detail a rename does leave -- a message naming
    the pre-rename stem -- was measured as unobservable in the verdicts.

    Only unambiguous cores enter the second map, and only for pages that are nodes at all:

    * two pages sharing a core name is a real defect -- the live wiki has 40 such families -- and
      resolving such a link to whichever page sorts first would hide it; the contested list keeps
      it visible instead (and the report names the candidates);
    * ``System_*`` pages and the non-node artifacts are excluded even though their files exist: a
      link resolving to one would satisfy the check while the indexer deletes that page from the
      graph, which is the same "hide the gap" trade the stub creator refuses when it declines to
      create one.  The exact-spelling route still finds them, as it always did.

    Measured on the live wiki: none of the links this rule rescues points at an ambiguous core,
    so refusing them costs nothing there.
    """
    core_pages: dict[str, list[str]] = defaultdict(list)
    for node_key in all_keys:
        if node_key.startswith("System_") or f"{node_key}.md" in NON_NODE_WIKI_FILES:
            continue
        core_pages[normalize_entity_name(strip_prefix(node_key))].append(node_key)
    return core_pages, {core: pages[0] for core, pages in core_pages.items() if len(pages) == 1}


def lint_vector_lake(auto_fix: bool = False):
    wiki_dir = str(get_wiki_dir())
    if not os.path.exists(wiki_dir):
        return "Wiki directory not found."

    # Linting scope and link resolution are two different questions, and one list used to
    # answer both.
    #
    # A file the wiki writes about itself is not a node, so it must not be *linted* --
    # checked for a valid prefix, an id, aliases, a type.  The set here held only the
    # first three of the six, so ``orphan_pages.md``, ``wiki_link_stats.md`` and
    # ``Synthesis_log.md`` were linted as nodes: reported as "Does not start with valid
    # prefix" and, under ``auto_fix``, renamed to ``Concept_orphan_pages.md``.
    #
    # But such a file does *exist*, so a link to it must *resolve*: otherwise the link is
    # reported broken and a stub is invented beside the real page.  Because both
    # questions read the same list, ``index``/``log``/``overview`` were exactly the three
    # names that could not resolve a link.
    listed = [name for name in os.listdir(wiki_dir) if name.endswith(".md")]
    files = [name for name in listed if name not in NON_NODE_WIKI_FILES]
    # Every vocabulary is imported from its single owner.  The local copies that
    # used to live here had already drifted: ``valid_status`` was missing
    # ``archived`` and ``contested``, so every page using those two legal statuses
    # was reported as an invalid status even though schema_validator accepts them.
    # tests/test_lint_vocabularies.py fails if a copy reappears.
    valid_types = VALID_TYPES
    valid_status = _LOWERCASE_STATUS
    valid_epistemic = VALID_EPISTEMIC_STATUS
    valid_categories = VALID_CATEGORIES
    valid_prefixes = VALID_PREFIXES
    required_fields = list(REQUIRED_FIELDS)

    files = [name for name in listed if name not in NON_NODE_WIKI_FILES]
    issues = {key: [] for key in ["frontmatter", "schema", "naming", "type_status", "category", "duplicate_id", "alias_conflict", "broken_links", "orphan", "similarity", "decay", "semantic_gc", "governance", "alignment"]}
    fixes_applied = 0
    stubs_refused = 0

    parsed = {}
    id_map = {}
    alias_map = {}
    all_keys = set()
    link_target_map = {}
    #: Declared names (titles and aliases) -> the pages claiming them, resolved after the parse
    #: loop so that only unambiguous declarations reach ``link_target_map``.
    declared: dict[str, list[str]] = defaultdict(list)
    #: The same claims keyed by *normalised* name, for deciding whether a name is contested.  Raw
    #: spelling cannot answer that: ``title: Atrium Health`` and ``[[Atrium-Health]]`` are one name
    #: (``_``/``-`/space), and looking it up literally both mislabelled the link "does not exist"
    #: and bypassed the auto-fix guard below -- which wrote a third page for a live contested name.
    declared_norm: dict[str, set[str]] = defaultdict(set)
    inbound_count = defaultdict(int)

    # Every page on disk is a link target, whether or not it is linted.
    for filename in listed:
        node_key = filename[:-3]
        all_keys.add(node_key)
        link_target_map[node_key] = node_key

    # A link may name a page by its *core* name -- ``[[Concept_CoMET]]`` where only
    # ``Product_CoMET.md`` exists.  That is the same rule the stub creator uses to decide
    # whether a page already covers a target, and without it lint reported live targets as
    # broken that a page already answers (24 distinct targets, 67 link occurrences), while
    # ``--auto-fix`` used to "fix" them by forking a second page beside the real one.
    #
    # Only unambiguous cores are added, and only for pages that are nodes at all.  Two pages
    # sharing a core name is a real defect -- the live wiki has 40 such families -- and
    # resolving such a link to whichever page sorts first would hide it; left unresolved, the
    # report keeps it visible (and now names the contested pages).  Measured on the live wiki,
    # none of the links this rule rescues points at an ambiguous core, so refusing them costs
    # nothing there.  Exact filenames, titles and aliases keep precedence.
    #
    # ``System_*`` pages and the non-node artifacts are left out of the core map even though
    # their files exist: a link resolving to one would satisfy this check while the indexer
    # deletes that page from the graph, which is the same "hide the gap" trade the stub creator
    # refuses when it declines to *create* one.  The exact-spelling route still finds them, as it
    # always did; this only declines to widen that.
    core_pages, unique_cores = core_name_maps(all_keys)
    # First Pass: Read and parse the files that are nodes
    for filename in files:
        filepath = os.path.join(wiki_dir, filename)
        node_key = filename[:-3]
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

        # Titles and aliases are *declarations*, and a declaration is only usable when exactly
        # one page makes it.  These used plain assignment, so a name two pages both claimed
        # resolved to whichever one ``os.listdir`` happened to read last -- and a declaration
        # that collided with an existing page's filename overwrote that page's own stem, which
        # made a link to a real file resolve to a different one.  Now: collect, then add only the
        # unambiguous ones, and never over a filename (``setdefault``).  A contested name stays
        # unresolved, which is the visible outcome the core-name rule already chose; the existing
        # alias-conflict check still reports it.
        title = frontmatter.get("title")
        if title:
            declared[str(title).strip()].append(node_key)
            declared_norm[normalize_entity_name(str(title).strip())].add(node_key)

        aliases = frontmatter.get("aliases", [])
        if isinstance(aliases, str):
            aliases = [aliases]
        if isinstance(aliases, list):
            for alias in aliases:
                alias_str = str(alias).strip()
                declared[alias_str].append(node_key)
                declared_norm[normalize_entity_name(alias_str)].add(node_key)
                alias_map.setdefault(alias_str, []).append(filename)

    # Only now can a declaration be judged: every page has been read, so "how many pages claim
    # this name" is known.  Unambiguous declarations join the map without displacing anything.
    for name, claimants in declared.items():
        if len(claimants) == 1:
            link_target_map.setdefault(name, claimants[0])

    for filename, data in parsed.items():
        for target in data["links"]:
            real_key = resolve_link_target(target, link_target_map, unique_cores)
            inbound_count[real_key or target] += 1

    # Apply Auto-fixes iteratively
    # 1. Naming Compliance
    renamed_files = {}
    for filename in files:
        if not filename.startswith(valid_prefixes):
            issues["naming"].append(f"{filename}: Does not start with valid prefix")
            if auto_fix:
                new_filename = f"Concept_{filename}"
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
                # The file exists under its new name now, and a filename outranks a declaration --
                # without this the rest of the pass would still let a declaration spelling it win.
                link_target_map[new_key] = new_key
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
                        parsed[fname]["fm"]["id"] = _generate_id(fname[:-3])
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
                            # The alias is gone from disk now, so it is no longer claimed here:
                            # otherwise one report says both "claim removed" and "two pages
                            # declare this name", about the same run.
                            if len(alias_map[alias]) == 2:
                                declared_norm.pop(normalize_entity_name(alias), None)

    # 4. Broken Links (Stub Creation)
    # Names two or more pages declare, computed *here* rather than before check 3: the alias
    # auto-fix above can strip the losing claim from disk, and a set captured earlier would still
    # call the name contested in the same run that reported removing the claim.
    contested_names = stub_creator.contested_names(declared_norm)
    # The stub index is built once for the pass and updated in place by the creator, so a
    # stub created for one link is not "missing" when the next link is looked at.
    stub_index = stub_creator.existence_index(wiki_dir)
    for filename, data in parsed.items():
        for target in data["links"]:
            if resolve_link_target(target, link_target_map, unique_cores) is None:
                # A contested core name is reported as broken, but saying only that leaves the
                # operator nothing to act on: the name is not unknown, it is ambiguous.  The
                # item stays in this bucket so the count means the same thing.
                contested = core_pages.get(normalize_entity_name(strip_prefix(target)))
                claimants = sorted(declared_norm.get(normalize_entity_name(target), ()))
                if contested and len(contested) > 1:
                    detail = (
                        f"target does not exist ({len(contested)} pages share that name: "
                        f"{', '.join(sorted(contested))})"
                    )
                elif len(claimants) > 1:
                    # A title or alias two pages both claim: not unknown, contested.
                    detail = (
                        f"target does not exist ({len(claimants)} pages declare that name: "
                        f"{', '.join(sorted(claimants))})"
                    )
                else:
                    detail = "target does not exist"
                issues["broken_links"].append(f"{filename} -> [[{target}]]: {detail}")
                if auto_fix:
                    # The filename prefix and the frontmatter ``type`` are one decision, and the
                    # type has to come from the vocabulary.  This used to be written out here,
                    # naming the file after the target's own prefix while always declaring
                    # ``concept``, so ``[[Vendor_X]]`` produced ``Vendor_X.md`` typed
                    # ``concept`` -- which the write refused, and the exception was caught and
                    # logged, so the link was simply never fixed and the report said nothing.
                    # What a stub is now belongs to one owner; see
                    # ``vector_lake.stub_creator`` for the rules and why each was chosen.
                    outcome = stub_creator.create_stub(
                        wiki_dir, target, stub_index, contested=contested_names
                    )
                    if outcome.refused:
                        stubs_refused += 1
                    elif outcome.stem:
                        all_keys.add(outcome.stem)
                        link_target_map[outcome.stem] = outcome.stem
                        # Keep the core map in step: within one pass, a link written before this
                        # stub must resolve against what is now on disk.
                        unique_cores.setdefault(
                            normalize_entity_name(strip_prefix(outcome.stem)), outcome.stem
                        )
                        fixes_applied += 1

    # 5. Frontmatter, Type, Status, Category
    for filename, data in parsed.items():
        frontmatter = data["fm"]
        changed = False

        missing = missing_required_fields(frontmatter, filename)
        if missing:
            issues["frontmatter"].append(f"{filename}: Missing fields: {', '.join(missing)}")
            if auto_fix:
                if not frontmatter.get("id"): frontmatter["id"] = _generate_id(node_key)
                if not frontmatter.get("title"): frontmatter["title"] = filename[:-3]
                if not frontmatter.get("type"): frontmatter["type"] = filename.split("_", 1)[0].lower()
                if not frontmatter.get("domain"): frontmatter["domain"] = "General"
                if not frontmatter.get("topic_cluster"): frontmatter["topic_cluster"] = "General"
                if not frontmatter.get("status"): frontmatter["status"] = "Active"
                if not frontmatter.get("epistemic-status"): frontmatter["epistemic-status"] = "seed"
                if not frontmatter.get("categories"): frontmatter["categories"] = ["Uncategorized"]
                if not frontmatter.get("updated"): frontmatter["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                if "sources" not in frontmatter: frontmatter["sources"] = []
                if not frontmatter.get("strategic_scope"): frontmatter["strategic_scope"] = "edge"
                if not frontmatter.get("evidence_tier"): frontmatter["evidence_tier"] = "derived"
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
        if isinstance(categories, str): categories = [categories]
        if isinstance(categories, list):
            # ``SCHEMA_CATEGORIES.md`` covers entities, concepts and synthesis nodes.
            # A derived system artifact is none of those, so its own marker category
            # is allowed -- and only there.
            system_artifact = filename.startswith("System_")
            new_cats = []
            for category in categories:
                if category not in valid_categories and not (
                    system_artifact and category in SYSTEM_ARTIFACT_CATEGORIES
                ):
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

        try:
            validate_schema(frontmatter, data["body"], filename)
        except SchemaViolationException as e:
            issues["schema"].append(f"{filename}: {str(e)}")

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
                    primary_order = establishment_key(fm_a.get("created"), fm_a.get("id") or key_a)
                    secondary_order = establishment_key(fm_b.get("created"), fm_b.get("id") or key_b)
                    
                    if secondary_order < primary_order:
                        primary, secondary = file_b, file_a
                        s_key = key_a
                    else:
                        primary, secondary = file_a, file_b
                        s_key = key_b
                    
                    p_data = parsed[primary]
                    s_data = parsed[secondary]
                    
                    # Body, aliases and the survivor's frontmatter come from the
                    # shared section-aware merger: a naive concatenation put the
                    # consumed page's compiled-truth bullets after
                    # `## 2. 证据时间线`, where the schema validator reads them as
                    # malformed timeline entries.
                    merged_content = merge_markdown_content(
                        _render_page(p_data["fm"], p_data["body"]),
                        _render_page(s_data["fm"], s_data["body"]),
                    )
                    merged_fm, merged_body = split_frontmatter(merged_content)
                    merged_fm["updated"] = datetime.datetime.now().strftime("%Y-%m-%d")

                    # Write Primary
                    _write_fixed_frontmatter(p_data["path"], merged_fm, merged_body)
                    
                    # Delete Secondary
                    try:
                        from vector_lake.mutation_coordinator import execute_mutation_plan
                        execute_mutation_plan(secondary, is_delete=True)
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
        "policy": 1095,
        "standard": 1095,
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
            if node_status == "active":
                issues["alignment"].append(f"{filename}: Alignment Score {alignment_score} < 60 while status is Active; review for Draft, Superseded, or Deprecated.")

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
        "schema": "14. Strict Schema Verification",
    }

    total_issues = sum(len(items) for items in issues.values())
    header = f"Scanned: {len(files)} files | Issues: {total_issues} | Auto-fixed: {fixes_applied}"
    if stubs_refused:
        # A refusal is a closed gate (no purpose contract, unreachable coordinator, a path
        # outside the wiki), so every stub in the pass was refused.  That is not the same as
        # "there was nothing to write", and a report that showed only "Auto-fixed: 0" would
        # read as the second.
        header += f" | Stub writes refused: {stubs_refused} (see the log)"
    lines = ["=== Vector Lake Lint Report ===", header, ""]
    for key, name in check_names.items():
        items = issues[key]
        lines.append(f"{name}: {'[PASS]' if not items else f'[FAIL: {len(items)}]'}")
        for item in items[:10]:
            lines.append(f"    {item}")
        if len(items) > 10:
            lines.append(f"    ... and {len(items) - 10} more")
        lines.append("")
    return "\n".join(lines)
