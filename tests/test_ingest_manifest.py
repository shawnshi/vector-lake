"""The ingest task packet's dispatch manifest.

The prompt tells the model to preserve ``source_projection_hash`` and
``ingest_contract_version`` verbatim and to use only relations listed in
``integration_candidates``.  None of those fields existed in the packet the producer built, so the
prompt described a manifest that was never there.  These tests pin the producer side: the manifest
is emitted, and the candidate list in it is the *same* list the prompt shows.
"""

import json
from pathlib import Path

import pytest

from vector_lake import db_store, governance_store, tool_ingest
from vector_lake.mutation_coordinator import execute_mutation_plan
from vector_lake.wiki_utils import projection_hash

from tests.test_ingest_contract import _concept_content
from tests.test_mutation_coordinator import _source_content, _write_purpose_contract

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_projection_hash_normalises_line_endings():
    """A CRLF checkout must not hash a different knowledge than an LF one."""
    assert projection_hash("a\r\nb\r\n") == projection_hash("a\nb\n")
    assert projection_hash("a\rb") == projection_hash("a\nb")


def test_projection_hash_tracks_content():
    assert projection_hash("a") != projection_hash("b")
    assert projection_hash("a") != projection_hash("a\n"), "a trailing newline is content"


def _raw_and_candidate(isolated_memory):
    """A raw source plus one registerable candidate page and its index entry."""
    _write_purpose_contract(isolated_memory)
    raw = isolated_memory / "raw" / "news" / "manifest.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        _source_content().replace(
            "Primary source content.",
            "Primary source content about the target concept and its mechanism.",
        ),
        encoding="utf-8",
    )
    execute_mutation_plan("Concept_Target.md", content=_concept_content())
    index_path = isolated_memory / "wiki" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps({
            "nodes": {
                "Concept_Target": {
                    "type": "concept",
                    "title": "Concept Target",
                    "summary": "Target compiled truth about the target concept.",
                }
            }
        }),
        encoding="utf-8",
    )
    return raw


def test_a_packet_carries_the_dispatch_manifest(isolated_memory):
    raw = _raw_and_candidate(isolated_memory)
    assert "enqueued 1" in tool_ingest.prepare_ingest_batch(batch_size=5)

    payload = json.loads(
        db_store.get_connection().execute(
            "SELECT payload FROM jobs WHERE task_type = 'ingest'"
        ).fetchone()[0]
    )

    assert payload["ingest_contract_version"] == tool_ingest.INGEST_CONTRACT_VERSION
    assert payload["source_projection_hash"] == projection_hash(raw.read_text(encoding="utf-8"))
    assert payload["integration_candidates"], "the candidate manifest is empty"


def test_the_prompt_and_the_manifest_come_from_one_calculation(isolated_memory):
    """The model may only name candidates it was shown, so the two must not be computed apart."""
    raw = _raw_and_candidate(isolated_memory)
    assert "enqueued 1" in tool_ingest.prepare_ingest_batch(batch_size=5)

    payload = json.loads(
        db_store.get_connection().execute(
            "SELECT payload FROM jobs WHERE task_type = 'ingest'"
        ).fetchone()[0]
    )
    candidates = payload["integration_candidates"]
    rendered = tool_ingest._render_index_context(candidates)

    assert f"- {json.dumps(candidates[0], ensure_ascii=False)}" in rendered
    assert rendered in payload["instructions"], "the prompt does not show the manifest's candidates"
    assert [c["target"] for c in candidates] == [
        c["target"] for c in tool_ingest.select_ingest_candidates(str(raw))
    ]


def test_candidate_records_carry_the_markdown_projection_hash(isolated_memory):
    """``target_projection_hash`` is the content baseline, derived from the canonical projection."""
    raw = _raw_and_candidate(isolated_memory)
    candidates = tool_ingest.select_ingest_candidates(str(raw))

    assert candidates, "expected at least one candidate"
    candidate = candidates[0]
    canonical = tool_ingest._read_canonical_target_content(
        candidate["target"], candidate["target_hash"]
    )
    assert candidate["target_projection_hash"] == projection_hash(canonical)
    assert candidate["target_projection_hash"] != "", "an empty baseline is not a baseline"


def test_the_renderer_and_the_context_builder_agree(isolated_memory):
    raw = _raw_and_candidate(isolated_memory)
    context, candidates = tool_ingest.ingest_context_and_candidates(str(raw))
    assert context == tool_ingest._render_index_context(candidates)
    assert tool_ingest._read_relevant_index_context(str(raw)) == context


def test_a_cold_knowledge_base_reports_no_candidates(isolated_memory):
    _write_purpose_contract(isolated_memory)
    raw = isolated_memory / "raw" / "news" / "cold.md"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(_source_content(), encoding="utf-8")

    context, candidates = tool_ingest.ingest_context_and_candidates(str(raw))
    assert candidates == []
    assert context == tool_ingest._COLD_KB_CONTEXT


# ---------------------------------------------------------------------------------------------
# Consumer side: the manifest is an allowlist, not documentation.
# ---------------------------------------------------------------------------------------------

_SOURCE_PAGE = "Source_Manifest.md"


def _relation(target, target_hash, *, projection=None):
    relation = {
        "target": target,
        "target_hash": target_hash,
        "predicate": "validates",
        "evidence": "The source directly supports the target mechanism.",
        "confidence": 0.9,
        "event_date": "2026-07-15",
        "event_tag": "Validation",
    }
    if projection is not None:
        relation["target_projection_hash"] = projection
    return relation


def _apply(isolated_memory, raw, *, candidates, relation, source_projection_hash=None, filepath=None):
    processed_data = {
        "filepath": filepath if filepath is not None else str(raw),
        "canonical_name": _SOURCE_PAGE,
        "source_hash": "source-version",
        "integration": {"disposition": "integrated", "relations": [relation]},
    }
    if candidates is not _ABSENT:
        processed_data["integration_candidates"] = candidates
    if source_projection_hash is not None:
        processed_data["source_projection_hash"] = source_projection_hash
    return tool_ingest._apply_integration_disposition(
        [{"filename": _SOURCE_PAGE, "content": _source_content()}], processed_data
    )


class _Absent:
    """Sentinel: the packet has no ``integration_candidates`` key at all."""


_ABSENT = _Absent()


def _manifest(raw):
    return tool_ingest.select_ingest_candidates(str(raw))


def test_a_relation_outside_the_manifest_is_refused(isolated_memory):
    raw = _raw_and_candidate(isolated_memory)
    candidate = _manifest(raw)[0]
    manifest = [{**candidate, "target": "Concept_Not-Offered.md"}]

    with pytest.raises(ValueError, match="not in the packet's candidate manifest"):
        _apply(isolated_memory, raw, candidates=manifest, relation=_relation(
            candidate["target"], candidate["target_hash"]))


def test_a_manifest_whose_token_disagrees_with_the_relation_is_refused(isolated_memory):
    """A tampered manifest must not widen what the relation may name."""
    raw = _raw_and_candidate(isolated_memory)
    candidate = _manifest(raw)[0]
    manifest = [{**candidate, "target_hash": "0" * 64}]

    with pytest.raises(ValueError, match="does not match the candidate manifest"):
        _apply(isolated_memory, raw, candidates=manifest, relation=_relation(
            candidate["target"], candidate["target_hash"]))


def test_a_stale_target_projection_hash_is_refused(isolated_memory):
    raw = _raw_and_candidate(isolated_memory)
    candidate = _manifest(raw)[0]

    with pytest.raises(ValueError, match="target_projection_hash is stale"):
        _apply(isolated_memory, raw, candidates=[candidate], relation=_relation(
            candidate["target"], candidate["target_hash"], projection="0" * 64))


def test_an_empty_manifest_permits_no_relation(isolated_memory):
    """``[]`` means "nothing to integrate with", not "no restrictions"."""
    raw = _raw_and_candidate(isolated_memory)
    candidate = _manifest(raw)[0]

    with pytest.raises(ValueError, match="not in the packet's candidate manifest"):
        _apply(isolated_memory, raw, candidates=[], relation=_relation(
            candidate["target"], candidate["target_hash"]))


def test_a_packet_without_a_manifest_is_refused_for_integrated(isolated_memory):
    """M1, fail-closed: the migration refuses rather than restoring the old allow-all.

    A pre-manifest packet is rebuilt and re-dispatched by ``requeue_legacy_ingest_jobs``, which
    keys on the contract version -- so refusing here always has a recovery path.
    """
    raw = _raw_and_candidate(isolated_memory)
    candidate = _manifest(raw)[0]

    with pytest.raises(ValueError, match="predates"):
        _apply(isolated_memory, raw, candidates=_ABSENT, relation=_relation(
            candidate["target"], candidate["target_hash"]))


def test_a_packet_without_a_manifest_is_fine_when_it_declares_no_relation(isolated_memory):
    """The manifest bounds relations, so a disposition that declares none needs no manifest."""
    raw = _raw_and_candidate(isolated_memory)
    processed_data = {
        "filepath": str(raw),
        "canonical_name": _SOURCE_PAGE,
        "source_hash": "source-version",
        "integration": {
            "disposition": "standalone",
            "reason": "Single-source compilation with no cross-page integration.",
        },
    }
    files, disposition = tool_ingest._apply_integration_disposition(
        [{"filename": _SOURCE_PAGE, "content": _source_content()}], processed_data
    )
    assert disposition == "standalone"
    assert [item["filename"] for item in files] == [_SOURCE_PAGE]


def test_the_legacy_requeue_rebuilds_a_packet_for_an_older_contract(isolated_memory):
    """The recovery path M1 depends on: a packet below the contract version is rebuilt.

    Observed on the live queue: 1730 ``jobs`` rows carry a payload written before the manifest,
    so without this the refusal above would strand them instead of migrating them.
    """
    raw = _raw_and_candidate(isolated_memory)
    payload = {
        "filepath": str(raw),
        "hash": "legacy-hash",
        "canonical_name": _SOURCE_PAGE,
        "instructions": "legacy",
    }
    db_store.enqueue_job("ingest", payload)
    for row in db_store.get_connection().execute("SELECT job_id FROM jobs"):
        db_store.mark_job_awaiting_subagent(row["job_id"], "")

    assert tool_ingest.requeue_legacy_ingest_jobs() == 1
    rebuilt = json.loads(
        db_store.get_connection().execute("SELECT payload FROM jobs").fetchone()[0]
    )
    assert rebuilt["ingest_contract_version"] == tool_ingest.INGEST_CONTRACT_VERSION
    assert rebuilt["integration_candidates"], "the rebuilt packet still has no manifest"
    assert rebuilt["source_projection_hash"]


def test_a_correct_manifest_relation_is_accepted(isolated_memory):
    """Positive control: the checks above must not refuse a conforming relation."""
    raw = _raw_and_candidate(isolated_memory)
    candidate = _manifest(raw)[0]
    files, _ = _apply(
        isolated_memory,
        raw,
        candidates=[candidate],
        relation=_relation(
            candidate["target"],
            candidate["target_hash"],
            projection=candidate["target_projection_hash"],
        ),
        source_projection_hash=projection_hash(raw.read_text(encoding="utf-8")),
    )
    assert any(item["filename"] == candidate["target"] for item in files)


def test_a_source_edited_after_the_packet_was_built_is_refused(isolated_memory):
    raw = _raw_and_candidate(isolated_memory)
    candidate = _manifest(raw)[0]
    stale = projection_hash(raw.read_text(encoding="utf-8"))
    raw.write_text(raw.read_text(encoding="utf-8") + "\nAn edit after dispatch.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="changed after the ingest packet was built"):
        _apply(
            isolated_memory,
            raw,
            candidates=[candidate],
            relation=_relation(candidate["target"], candidate["target_hash"]),
            source_projection_hash=stale,
        )


# ---------------------------------------------------------------------------------------------
# The runner's fallback must stack under the model's disposition, not replace it.
# ---------------------------------------------------------------------------------------------


def test_the_runner_stacks_its_fallback_under_the_models_disposition():
    """The call site built ``{**processed, "integration": {...}}`` and silently dropped relations."""
    from scripts.ingest_runner import _with_disposition

    relations = [{"target": "Concept_Target.md", "target_hash": "version-token"}]
    merged = _with_disposition({
        "filepath": "raw/x.md",
        "integration": {"disposition": "integrated", "relations": relations},
    })

    assert merged["integration"]["disposition"] == "integrated"
    assert merged["integration"]["relations"] == relations
    assert merged["filepath"] == "raw/x.md", "stacking must not drop the packet fields"


def test_the_runner_supplies_its_fallback_only_when_the_model_declared_none():
    from scripts.ingest_runner import STANDALONE_FALLBACK_REASON, _with_disposition

    merged = _with_disposition({"filepath": "raw/x.md"})
    assert merged["integration"] == {
        "disposition": "standalone",
        "reason": STANDALONE_FALLBACK_REASON,
    }


def test_the_runner_repairs_a_standalone_that_carries_no_auditable_reason():
    """The finalizer enforces a 12-character reason floor; the runner fills it rather than fail."""
    from scripts.ingest_runner import STANDALONE_FALLBACK_REASON, _with_disposition

    merged = _with_disposition({"integration": {"disposition": "standalone", "reason": "short"}})
    assert merged["integration"]["reason"] == STANDALONE_FALLBACK_REASON


def test_the_runner_overrides_a_disposition_that_is_not_an_object():
    from scripts.ingest_runner import _with_disposition

    assert _with_disposition({"integration": "integrated"})["integration"]["disposition"] == "standalone"


# ---------------------------------------------------------------------------------------------
# Prompt and validator are two owners of one rule.
# ---------------------------------------------------------------------------------------------


def test_the_prompt_states_the_manifest_rule_the_finalizer_enforces():
    """The rule the refusals above implement has to be legible to the model, too."""
    template = (REPO_ROOT / "templates" / "ingest_prompt.md").read_text(encoding="utf-8")

    assert "integration_candidates" in template
    assert "empty manifest permits none" in template
    assert "cannot use `integrated`" in template
    assert "target_projection_hash" in template
    assert "source_projection_hash" in template


def test_the_prompt_does_not_promise_a_finalizer_that_never_reads_the_source():
    """It re-reads the claimed source to verify ``source_projection_hash``; saying otherwise is false."""
    template = (REPO_ROOT / "templates" / "ingest_prompt.md").read_text(encoding="utf-8")

    assert "Never return or ask the finalizer to read a `filepath`" not in template
    assert "verify `source_projection_hash`" in template
