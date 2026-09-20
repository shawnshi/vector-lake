"""The query tool's delivered payload and its write discipline.

Two defects lived at the same seam -- the one place the assembled context actually reaches the
model:

* diagnostics were computed and dropped, so a failed vector retrieval or a budget eviction looked
  exactly like a complete context;
* ``finalize_query_synthesis`` repaired a non-conforming page name with a bare ``os.rename``,
  bypassing the coordinator whose validator would have refused the name it invented.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

from vector_lake import get_extension_root, tool_query
from vector_lake.wiki_utils import validate_wiki_filename

from tests.test_ingest_contract import _concept_content
from tests.test_mutation_coordinator import _write_purpose_contract


def _context(**overrides):
    """A complete ``assemble_context`` result, shaped like the real one."""
    context = {
        "memory_packet": "MEMORY-BODY",
        "memory_count": 1,
        "memory_warning_count": 0,
        "memory_omitted_count": 0,
        "wiki_context": "WIKI-BODY",
        "wiki_page_count": 1,
        "index_summary": "INDEX-BODY",
        "purpose": "",
        "retrieval_notes": [],
        "budget_used": 100,
        "budget_max": 200000,
    }
    context.update(overrides)
    return context


def _delivered_payload(monkeypatch, context) -> str:
    monkeypatch.setattr(tool_query, "assemble_context", lambda query: context)
    instructions = tool_query.prepare_query_context("q")
    match = re.search(r"query_context_[0-9a-f]{12}\.md", instructions)
    assert match, f"the prompt does not name its payload file: {instructions[:200]}"
    return (get_extension_root() / "tmp" / match.group(0)).read_text(encoding="utf-8")


def test_the_index_summary_is_actually_delivered(monkeypatch):
    """It was computed, charged to ``budget_used``, and never put in the payload."""
    assert "INDEX-BODY" in _delivered_payload(monkeypatch, _context())


def test_a_retrieval_failure_is_visible_in_the_payload(monkeypatch):
    payload = _delivered_payload(
        monkeypatch,
        _context(retrieval_notes=["vector retrieval failed: provider timeout"]),
    )
    assert "RETRIEVAL NOTES" in payload
    assert "vector retrieval failed: provider timeout" in payload


def test_dropped_memory_items_are_visible_in_the_payload(monkeypatch):
    payload = _delivered_payload(monkeypatch, _context(memory_omitted_count=7))
    assert "7 operational memory item(s) were dropped" in payload


def test_a_clean_context_gets_no_degradation_section(monkeypatch):
    """The note must mean something: it cannot be printed unconditionally."""
    payload = _delivered_payload(monkeypatch, _context())
    assert "RETRIEVAL NOTES" not in payload


@pytest.mark.parametrize("name", ["bad-name.md", "concept_lowercase.md", "Note.md"])
def test_a_name_the_validator_refuses_is_withheld_not_renamed(isolated_memory, name):
    """``Orphan_<name>.md`` is not in ``VALID_PREFIXES``: the repair produced an invalid name."""
    wiki = isolated_memory / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / name).write_text("---\nid: x\n---\nbody\n", encoding="utf-8")
    with pytest.raises(ValueError):
        validate_wiki_filename(name)

    result = tool_query.finalize_query_synthesis(name, "q")

    assert not (wiki / f"Orphan_{name}").exists(), "the bypassing rename is back"
    assert (wiki / name).exists(), "the caller's file was moved behind the coordinator"
    assert "no valid wiki files synced" in result, result


def test_a_conforming_name_is_still_synced(isolated_memory):
    """Positive control: withholding must not swallow the ordinary path."""
    _write_purpose_contract(isolated_memory)
    wiki = isolated_memory / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "Concept_Target.md").write_text(_concept_content(), encoding="utf-8")
    validate_wiki_filename("Concept_Target.md")

    result = tool_query.finalize_query_synthesis("Concept_Target.md", "q")

    assert "1 page(s) verified present" in result, result


# ---------------------------------------------------------------------------------------------
# The prompt template fails closed, and the payload directory cannot grow without bound.
# ---------------------------------------------------------------------------------------------


def test_a_missing_template_fails_instead_of_becoming_the_instruction(tmp_path, monkeypatch):
    """It used to return ``Error: templates/query_prompt.md not found.`` as the prompt itself."""
    monkeypatch.setattr(tool_query, "get_extension_root", lambda: tmp_path)
    monkeypatch.setattr(tool_query, "assemble_context", lambda query: _context())

    with pytest.raises(FileNotFoundError, match="query_prompt.md"):
        tool_query.prepare_query_context("q")


def test_the_template_is_validated_before_the_payload_is_written(tmp_path, monkeypatch):
    """Ordering guarantee: a failed build must not leave a payload behind."""
    monkeypatch.setattr(tool_query, "get_extension_root", lambda: tmp_path)
    monkeypatch.setattr(tool_query, "assemble_context", lambda query: _context())

    with pytest.raises(FileNotFoundError):
        tool_query.prepare_query_context("q")

    leftovers = list((tmp_path / "tmp").glob("query_context_*.md")) if (tmp_path / "tmp").exists() else []
    assert leftovers == [], f"orphan payload left by a failed query: {leftovers}"


def test_a_present_template_still_renders(tmp_path, monkeypatch):
    """Positive control for the fail-closed change."""
    (tmp_path / "templates").mkdir(parents=True)
    (tmp_path / "templates" / "query_prompt.md").write_text(
        "Q={{query_str}} P={{payload_path}}", encoding="utf-8"
    )
    monkeypatch.setattr(tool_query, "get_extension_root", lambda: tmp_path)
    monkeypatch.setattr(tool_query, "assemble_context", lambda query: _context())

    rendered = tool_query.prepare_query_context("hello")

    assert rendered.startswith("Q=hello P=")
    assert "{{" not in rendered, "a placeholder was left unsubstituted"


def test_stale_query_payloads_are_pruned_and_fresh_ones_are_kept(tmp_path):
    """The directory grew forever: the only reference to the name was the line creating it."""
    import os
    import time as _time

    tmp = tmp_path / "tmp"
    tmp.mkdir()
    stale = tmp / "query_context_000000000000.md"
    fresh = tmp / "query_context_111111111111.md"
    unrelated = tmp / "keep-me.txt"
    for path in (stale, fresh, unrelated):
        path.write_text("x", encoding="utf-8")
    old = _time.time() - (tool_query.QUERY_CONTEXT_TTL_SECONDS + 60)
    os.utime(stale, (old, old))

    tool_query._prune_stale_query_contexts(tmp)

    assert not stale.exists(), "a payload past its TTL survived"
    assert fresh.exists(), "a payload inside its TTL was removed"
    assert unrelated.exists(), "pruning must not touch files it does not own"


def test_pruning_a_missing_directory_is_not_fatal(tmp_path):
    """Housekeeping must never cost the caller its query."""
    tool_query._prune_stale_query_contexts(tmp_path / "absent")


def test_finalization_no_longer_claims_a_change_set_it_never_made(isolated_memory):
    """It printed ``mutation_coordinator_handled`` while never calling the coordinator."""
    _write_purpose_contract(isolated_memory)
    wiki = isolated_memory / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "Concept_Target.md").write_text(_concept_content(), encoding="utf-8")

    result = tool_query.finalize_query_synthesis("Concept_Target.md", "q")

    assert "mutation_coordinator_handled" not in result, result


def test_a_named_file_that_is_absent_is_reported(isolated_memory):
    """Absent names used to be skipped in silence, so the caller saw a clean summary."""
    _write_purpose_contract(isolated_memory)
    (isolated_memory / "wiki").mkdir(parents=True, exist_ok=True)

    result = tool_query.finalize_query_synthesis("Concept_Missing.md", "q")

    assert "no valid wiki files synced" in result, result


def test_an_absent_name_beside_a_present_one_is_counted(isolated_memory):
    _write_purpose_contract(isolated_memory)
    wiki = isolated_memory / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (wiki / "Concept_Target.md").write_text(_concept_content(), encoding="utf-8")

    result = tool_query.finalize_query_synthesis("Concept_Target.md,Concept_Missing.md", "q")

    assert "1 named file(s) were absent" in result, result


def test_the_memory_share_is_nominal_and_the_burst_needs_an_alert(isolated_memory, monkeypatch):
    """Memory took a hardcoded 0.50 of every budget; the declared share is 0.30."""
    from vector_lake import tool_search

    calls = []

    def fake_packet(query, max_chars=60000):
        calls.append(max_chars)
        return {"packet": "m", "memory_count": 1, "warning_count": 0, "omitted_count": 0}

    monkeypatch.setattr(tool_search, "build_memory_packet", fake_packet)
    tool_search.assemble_context("q", max_chars=200000)
    assert calls == [60000], f"expected only the nominal share, got {calls}"

    calls.clear()

    def alerting_packet(query, max_chars=60000):
        calls.append(max_chars)
        return {"packet": "m", "memory_count": 1, "warning_count": 2, "omitted_count": 0}

    monkeypatch.setattr(tool_search, "build_memory_packet", alerting_packet)
    tool_search.assemble_context("q", max_chars=200000)
    assert calls == [60000, 100000], f"expected nominal then burst, got {calls}"


# ---------------------------------------------------------------------------------------------
# Comparative detection, and the wiki budget recycling its neighbours' leftovers.
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["always worth reading", "canvas layout", "reviews of it", "obviously"])
def test_an_ordinary_word_containing_vs_is_not_a_comparative_query(monkeypatch, query):
    """``"vs" in query_str`` fired on all of these and prefixed a bogus SYSTEM NOTE."""
    assert not tool_query.COMPARATIVE_QUERY_PATTERN.search(query)


@pytest.mark.parametrize(
    "query", ["A vs B", "A vs. B", "A versus B", "甲 对比 乙", "VS the baseline"]
)
def test_a_real_comparison_is_still_detected(query):
    assert tool_query.COMPARATIVE_QUERY_PATTERN.search(query)


def _budget_run(isolated_memory, monkeypatch, catalog_nodes: int, max_chars: int = 200000):
    """Run ``assemble_context`` with a controlled catalog and 20 equal-size candidate pages."""
    import json

    from vector_lake import page_index_projection, tool_search

    wiki = isolated_memory / "wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    # The summary branch is gated on the index existing; without it both runs produce "" and the
    # differential below would pass for the wrong reason.
    (wiki / "index.json").write_text("{}", encoding="utf-8")
    pages = []
    for index in range(80):
        key = f"Concept_Page{index:02d}"
        (wiki / f"{key}.md").write_text("x" * 3000, encoding="utf-8")
        pages.append((1.0, {"_key": key, "title": key}))

    class Catalog:
        def node_summary_lines(self, limit):
            return [f"{'n' * 40}" for _ in range(min(limit, catalog_nodes))]

    monkeypatch.setattr(page_index_projection, "read_catalog", lambda: (Catalog(), None))
    monkeypatch.setattr(tool_search, "_search_scored_pages", lambda *a, **k: (pages, [], None))
    monkeypatch.setattr(
        tool_search,
        "build_memory_packet",
        lambda query, max_chars=60000: {
            "packet": "m",
            "memory_count": 1,
            "warning_count": 0,
            "omitted_count": 0,
        },
    )
    monkeypatch.setattr(
        "vector_lake.purpose_contract.render_strategy_directive", lambda: ""
    )
    return tool_search.assemble_context("q", max_chars=max_chars)


def test_a_smaller_index_summary_gives_the_wiki_more_room(isolated_memory, monkeypatch):
    """The wiki budget charged the index *allocation*, so a small catalog's leftover was lost."""
    small = _budget_run(isolated_memory, monkeypatch, catalog_nodes=1)
    large = _budget_run(isolated_memory, monkeypatch, catalog_nodes=200)

    assert small["wiki_page_count"] > large["wiki_page_count"], (
        f"catalog size does not affect the wiki budget: {small['wiki_page_count']} vs "
        f"{large['wiki_page_count']}"
    )
    # The allocation-based cap was ``max - memory - 0.05*max - 0.15*max`` = 0.80 * max, so
    # anything above that proves the leftover is now recycled rather than lost.
    assert len(small["wiki_context"]) > 0.80 * 200000
    assert small["budget_used"] <= small["budget_max"]


def test_the_budget_invariant_survives_recycling(isolated_memory, monkeypatch):
    for max_chars in (2000, 12000, 30000, 200000):
        context = _budget_run(isolated_memory, monkeypatch, catalog_nodes=200, max_chars=max_chars)
        assert context["budget_used"] <= context["budget_max"], max_chars


# ---------------------------------------------------------------------------------------------
# The prompt's placeholders and the budget table are each owned in two places.
# ---------------------------------------------------------------------------------------------


def test_the_template_and_the_code_agree_on_placeholders():
    """``{{wiki_dir}}`` was substituted into a template that never contained it.

    Both directions matter: a placeholder the code does not handle would ship unsubstituted, and
    one the code handles but the template does not contain is dead weight that hides the drift.
    """
    template = (REPO_ROOT / "templates" / "query_prompt.md").read_text(encoding="utf-8")
    assert set(re.findall(r"\{\{([a-z_]+)\}\}", template)) == {"payload_path", "query_str"}


def test_a_rendered_prompt_has_no_unsubstituted_placeholder(tmp_path, monkeypatch):
    (tmp_path / "templates").mkdir(parents=True)
    (tmp_path / "templates" / "query_prompt.md").write_text(
        "{{query_str}}|{{payload_path}}", encoding="utf-8"
    )
    monkeypatch.setattr(tool_query, "get_extension_root", lambda: tmp_path)
    monkeypatch.setattr(tool_query, "assemble_context", lambda query: _context())

    rendered = tool_query.prepare_query_context("q")

    assert "{{" not in rendered and "}}" not in rendered, rendered


def test_the_budget_table_carries_every_share_the_assembler_reads():
    """A share the assembler reads but the table omits is a KeyError at query time."""
    from vector_lake import tool_search

    for key in ("operational_memory", "memory_burst", "index_summary", "system_prompt"):
        assert key in tool_search.BUDGET_SHARES, key
    shares = tool_search.BUDGET_SHARES
    assert all(0 < value <= 1 for value in shares.values()), shares
    assert shares["memory_burst"] >= shares["operational_memory"], (
        "the burst ceiling must not be below the nominal share"
    )


def test_the_prompt_does_not_claim_an_admission_gate_the_code_does_not_have():
    """The header named an env var that appears nowhere in the code but the template.

    A reader takes that for protection that exists, which is worse than an absent gate.
    """
    template = (REPO_ROOT / "templates" / "query_prompt.md").read_text(encoding="utf-8")

    assert "VECTOR_LAKE_ALLOW_MANUAL_QUERY_SYNTHESIS" not in template
    assert "outside the default read-only query path" not in template
    assert "{{payload_path}}" in template, "the placeholders must survive the rewrite"


def test_the_named_gate_really_is_absent_from_the_code():
    """If a gate is ever added, this test says so instead of the wording quietly drifting again.

    Scoped to the shipped paths on purpose: this test file names the variable, so an unscoped
    ``git grep`` would find the test itself the moment it is committed.
    """
    import subprocess

    search = subprocess.run(
        [
            "git",
            "grep",
            "-l",
            "VECTOR_LAKE_ALLOW_MANUAL_QUERY_SYNTHESIS",
            "--",
            "vector_lake/",
            "templates/",
            "scripts/",
            "skills/",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert search.stdout.strip() == "", (
        f"the variable is back in the shipped paths: {search.stdout.strip()}"
    )


def test_the_prompt_does_not_promise_checks_the_finalizer_does_not_perform():
    """``finalize_query_synthesis`` takes filenames and a query string -- nothing else.

    It enforces containment, the filename rule, the schema gate and stub gates. It has no nonce,
    no query hash, no prepared baseline, no content-hash comparison and no stub cap, so the
    prompt must not tell a reader that it does.
    """
    template = (REPO_ROOT / "templates" / "query_prompt.md").read_text(encoding="utf-8")

    # The defect was a promise of enforcement, so the test targets the promise form.  A bare
    # mention of a nonce is legitimate: line 25 forbids the *model* from inventing one, which is a
    # constraint on the model rather than a claim about what the controller checks.
    assert "will reject" not in template
    # The absence has to be stated, not left for the reader to infer.
    assert "no nonce" in template
    # It still has to describe the gates that do exist.
    assert "schema gate" in template
    assert "wiki directory" in template


def test_the_finalizer_signature_still_takes_only_filenames_and_a_query(
    isolated_memory, monkeypatch
):
    """Guard on the other side: if a nonce ever arrives, this fails and the template is revised."""
    import inspect

    parameters = list(inspect.signature(tool_query.finalize_query_synthesis).parameters)
    assert parameters == ["files_written_str", "query_str"], parameters
