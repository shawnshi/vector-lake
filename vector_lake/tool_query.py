import hashlib
import logging
import os
import re
import time

from vector_lake import get_extension_root, provenance
from vector_lake.tool_search import assemble_context
from vector_lake import stub_creator
from vector_lake.wiki_utils import (
    get_wiki_dir,
    normalize_entity_name,
    sanitize_wiki_node,
    validate_wiki_filename,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("vector-lake-tool-query")

# This file used to keep its own copy of the prefix list, and a second, inline copy of
# the same list further down.  Both omitted ``System_`` while the comment claimed to
# mirror ``wiki_utils.VALID_PREFIXES``.  The vocabulary now has one owner, and so does what a
# stub is: ``node_vocabulary`` for the
# prefixes and types, ``stub_creator`` for the page a broken link needs.  This file used to
# keep its own copy of the prefix list, a second inline copy of it, a stub writer, and a
# covering-page check that only it had -- which is why ``tool_lint`` forked entities that this
# file refused to fork.


QUERY_CONTEXT_TTL_SECONDS = 7 * 24 * 3600


def _prune_stale_query_contexts(tmp_dir) -> None:
    """Drop payload files left by finished queries, so the directory cannot grow without bound.

    Every call to :func:`prepare_query_context` writes a payload and nothing used to remove it:
    the only reference to the name anywhere in the tree was the line that created it, and 24 files
    had accumulated.  Pruning runs *before* this query's payload is written, so a file a live
    reader may still be holding is never removed underneath it, and the window is generous enough
    that an agent's own follow-up read is unaffected.

    Housekeeping only: a failure degrades to a warning and must never cost the caller its query.
    """
    cutoff = time.time() - QUERY_CONTEXT_TTL_SECONDS
    try:
        stale = [
            entry
            for entry in tmp_dir.glob("query_context_*.md")
            if entry.stat().st_mtime < cutoff
        ]
    except OSError as exc:
        log.warning("Could not scan %s for stale query payloads: %s", tmp_dir, exc)
        return
    for entry in stale:
        try:
            entry.unlink()
        except OSError as exc:
            log.warning("Could not remove stale query payload %s: %s", entry.name, exc)


COMPARATIVE_QUERY_PATTERN = re.compile(
    # A word-boundary match, because ``"vs" in query_str`` also fires on "always", "canvas",
    # "reviews" and "obvious".  This is a prompt hint only: the comment it replaced called this
    # "V11.2 Multi-Hop Parallel Retrieval", but no parallel retrieval exists here -- the flag adds
    # one sentence asking the model to weight both sides evenly.
    r"(?<![a-z0-9])vs\.?(?![a-z0-9])|versus|对比",
    re.IGNORECASE,
)


def prepare_query_context(query_str: str, dry_run: bool = False):
    # ``wiki_dir`` was read only by the dead ``{{wiki_dir}}`` substitution at the end of this
    # function; the template never contained that placeholder.
    
    is_comparative = bool(COMPARATIVE_QUERY_PATTERN.search(query_str))
    
    context = assemble_context(query_str)
    context_block = ""
    
    if is_comparative:
        context_block += "\n[SYSTEM NOTE: This is a comparative query. Ensure equal retrieval weighting for both sides to avoid skew.]\n"
    if context.get("memory_packet"):
        context_block += (
            f"\n\n--- OPERATIONAL MEMORY PACKET "
            f"({context.get('memory_count', 0)} items, {context.get('memory_warning_count', 0)} warnings) ---\n"
            f"{context['memory_packet']}"
        )
    if context["wiki_context"]:
        context_block += (
            f"\n\n--- RELEVANT WIKI PAGES ({context['wiki_page_count']} pages, "
            f"{context['budget_used']}/{context['budget_max']} chars) ---\n{context['wiki_context']}"
        )
    if context.get("index_summary"):
        context_block += f"\n\n--- NODE INDEX SUMMARY ---\n{context['index_summary']}"

    # Computed diagnostics used to be dropped here, so a vector-retrieval failure or a budget
    # eviction looked exactly like a complete context -- the silent-degradation shape this tree
    # forbids elsewhere.  ``budget_used`` already charges for these bytes, so surfacing them
    # makes the payload match what the accounting claims was delivered.
    retrieval_notes = [str(note) for note in (context.get("retrieval_notes") or []) if note]
    omitted_memory = int(context.get("memory_omitted_count") or 0)
    if retrieval_notes or omitted_memory:
        degradations = [f"- {note}" for note in retrieval_notes]
        if omitted_memory:
            degradations.append(
                f"- {omitted_memory} operational memory item(s) were dropped to fit the budget."
            )
        context_block += "\n\n--- RETRIEVAL NOTES (degradations) ---\n" + "\n".join(degradations)

    if context["purpose"]:
        context_block += f"\n\n--- PURPOSE ---\n{context['purpose']}"

    # The template is resolved and read *before* the payload is written.  It used to be read
    # afterwards, and a missing template merely set the template text to its own error message --
    # which was then returned as the instruction with no placeholder substituted at all.  The
    # ingest prompt builder already resolves this by raising; the query path was the outlier.
    templates_dir = get_extension_root() / "templates"
    prompt_path = templates_dir / "query_prompt.md"
    if not prompt_path.exists():
        raise FileNotFoundError("templates/query_prompt.md not found")
    prompt_template = prompt_path.read_text(encoding="utf-8")

    # Write context to a temporary payload file
    tmp_dir = get_extension_root() / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    _prune_stale_query_contexts(tmp_dir)
    
    # Create a unique hash for this query with anti-collision
    import uuid
    unique_str = f"{query_str}_{time.time()}_{uuid.uuid4().hex}"
    query_hash = hashlib.md5(unique_str.encode("utf-8")).hexdigest()[:12]
    payload_path = tmp_dir / f"query_context_{query_hash}.md"
    
    with open(payload_path, "w", encoding="utf-8") as f:
        f.write(context_block)
        
    if dry_run:
        trace = provenance.format_trace(provenance.build_trace_for_query(query_str))
        return f"[DRY RUN] Context assembled at {payload_path}\n\nTrace:\n{trace}"

    # ``{{wiki_dir}}`` used to be substituted into a template that never contained it. It is
    # removed rather than given a home in the template: this prompt's own trust model forbids the
    # model from naming paths at all ("Do not return paths..."), so handing it a directory to
    # write into would contradict the contract it is being asked to honour.
    instructions = prompt_template.replace("{{payload_path}}", str(payload_path)) \
        .replace("{{query_str}}", query_str)
        
    return instructions

def finalize_query_synthesis(files_written_str: str, query_str: str) -> str:
    if not files_written_str.strip():
        return "No files were written."
        
    wiki_dir = str(get_wiki_dir())
    changed_node_files = set([f.strip() for f in files_written_str.split(",") if f.strip()])
    
    valid_files = set()
    absent: list[str] = []
    import pathlib
    wiki_path = pathlib.Path(wiki_dir).resolve()
    
    for filename in changed_node_files:
        # Boundary check to prevent path traversal
        try:
            target_path = (wiki_path / filename).resolve()
            if not target_path.is_relative_to(wiki_path):
                log.warning(f"Security: Path traversal attempt detected: {filename}")
                continue
        except Exception as e:
            log.warning(f"Security: Invalid path {filename}: {e}")
            continue

        # The prefix rule defers to its single owner (``node_vocabulary`` via
        # ``validate_wiki_filename``).  This used to re-derive the rule inline as
        # ``filename.split('_')[0].isupper()`` and then *repair* the name with a bare
        # ``os.rename`` -- a raw filesystem write with no CAS, no outbox row, no change set and no
        # lease, in a tree whose safety model is that writes go through ``execute_mutation_plan``.
        # The repair was also wrong on its own terms: it produced ``Orphan_<name>.md``, and
        # ``Orphan_`` is not in ``VALID_PREFIXES``, so the coordinator's own validator would have
        # refused the name it invented.  A name the canonical validator rejects is now withheld
        # and reported rather than silently rewritten behind the coordinator's back.
        try:
            validate_wiki_filename(filename)
        except ValueError as exc:
            log.warning(
                "Query finalization withheld %s: %s. Rename it through ``rename_entity``, which "
                "also rewrites the internal links a bare rename leaves dangling.",
                filename,
                exc,
            )
            continue

        file_path = os.path.join(wiki_dir, filename)
        if os.path.exists(file_path):
            valid_files.add(filename)
            sanitize_wiki_node(file_path)
        else:
            absent.append(filename)
            
    if valid_files:
        # ``valid_files`` means "present on disk", nothing more.  The write belongs to the subagent,
        # so this function cannot attest a change set: it used to print
        # "Canonical change set: mutation_coordinator_handled" while never calling the coordinator.
        #
        # A canonical-version probe was tried as the missing check and **rejected**: a page written
        # with a plain ``write_text`` still reports a version, because the version is derived from
        # the page's own extracted entities rather than from a write record.  It would have passed
        # unconditionally while claiming to prove provenance.  What is reported here is therefore
        # only what was actually observed -- presence, stub counts, and absent names.
        stubs_created, stubs_refused = _generate_stubs_for_broken_links(wiki_dir, valid_files)
        trace = provenance.format_trace(provenance.build_trace_for_query(query_str))

        notes = []
        if stubs_refused:
            notes.append(f"{stubs_refused} stub write(s) were refused -- a gate rejected them (see the log).")
        if absent:
            notes.append(f"{len(absent)} named file(s) were absent from the wiki: {', '.join(absent[:3])}.")

        return (
            f"Query finalization completed. {len(valid_files)} page(s) verified present. "
            f"{stubs_created} stub(s) generated."
            + (" " + " ".join(notes) if notes else "")
            + f"\n\n{trace}"
        )
    return "Query finalization completed with no valid wiki files synced."


def _generate_stubs_for_broken_links(wiki_dir: str, files_to_scan: set) -> tuple[int, int]:
    """``(created, refused)`` for the broken links in ``files_to_scan``.

    ``refused`` counts writes a gate rejected -- as opposed to names that needed no page, which
    are the normal case and are counted nowhere.  Before this split, a run in which every write
    was refused returned 0 and read exactly like a wiki with nothing to fix.
    """
    existing_files, normalized_existing, existing_cores = stub_creator.existence_index(wiki_dir)
    existing = (existing_files, normalized_existing, existing_cores)
    broken_targets = set()

    for filename in files_to_scan:
        filepath = os.path.join(wiki_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as handle:
                content = handle.read()
        except Exception:
            continue

        # P1-1: Pre-strip code blocks to avoid fragile stubbing
        content = re.sub(r'```.*?```', '', content, flags=re.DOTALL)
        content = re.sub(r'`.*?`', '', content)

        for match in re.finditer(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]", content):
            raw_target = match.group(1).strip().replace(".md", "")
            target = normalize_entity_name(raw_target)
            covering = stub_creator.covering_page(target, existing)
            if covering:
                if covering != target:
                    log.warning(
                        "Not creating %s.md: %s already covers that name; fix the link instead.",
                        target,
                        covering,
                    )
                continue
            broken_targets.add(target)
        for match in re.finditer(r"\[[^\[\]]+?::\s*\[\[([^\]]+?)\]\]\]", content):
            raw_target = match.group(1).strip().split("|")[0].strip().replace(".md", "")
            target = normalize_entity_name(raw_target)
            covering = stub_creator.covering_page(target, existing)
            if covering:
                if covering != target:
                    log.warning(
                        "Not creating %s.md: %s already covers that name; fix the link instead.",
                        target,
                        covering,
                    )
                continue
            broken_targets.add(target)

    if not broken_targets:
        return 0, 0

    stubs = 0
    refused = 0
    # A name two pages declare is contested, not missing: the owner refuses it, and this caller
    # has to say so too -- it writes stubs as well, and with neither claimant's core matching the
    # name, ``covering_page`` cannot refuse for it.  Measured on the live wiki: 31 such names.
    contested = stub_creator.contested_names(stub_creator.declared_names(wiki_dir))
    # What a stub is -- its name, type, fields and the write path -- belongs to one owner.
    # This used to build the page here and hand ``<target>.md`` to ``execute_mutation_plan``,
    # which no node may be named: the write was refused and the exception swallowed, so this
    # function created no stub at all for an untyped target (and, as it turned out, none for
    # a typed one either).  See ``vector_lake.stub_creator``.
    #
    # ``index`` is the one built before the scan, not a fresh one: ``create_stub`` updates it
    # in place, so a stub written for one target is seen as covering its own name by the next
    # iteration.  A second index here would leave that update on a set nothing reads again.
    for target in sorted(broken_targets):
        outcome = stub_creator.create_stub(wiki_dir, target, existing, contested=contested)
        if outcome.stem:
            stubs += 1
        elif outcome.refused:
            refused += 1
    if refused:
        # The count is about closed gates, not about missing links; folding it into ``stubs``
        # would hide a run in which nothing could be written.
        log.error(
            "%s stub write(s) refused: every stub in this pass failed the same gate "
            "(the log above has the traceback).",
            refused,
        )
    return stubs, refused

