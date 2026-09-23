"""The rule the model kept violating, and the dead letter for a source that keeps failing it.

Two defects from the 2026-09-19 live audit, both visible on one file
(``raw/DigitalHealthWeeklyBrief/DHWB-20260913.md``):

* **the content rule was not stated where the model could read it.** The validator requires
  ``categories`` to be a list with *exactly one* element; the ingest prompt said "using EXACTLY
  one of the 8 macro-domains", which a model can satisfy with ``categories: Healthcare_IT`` or a
  two-element list -- and both are refused. Six days of rounds were spent rediscovering that.
* **"bounded attempts" bounded one job, not the source.** Each re-dispatch got a fresh budget, so
  the source cycled forever at three model calls per round. The catch-up sweep now withholds a
  source whose *content* keeps failing deterministically, keyed on the content hash so that a
  corrected file becomes dispatchable again by itself.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import yaml

from vector_lake import db_store, tool_ingest
from vector_lake.purpose_contract import PurposeContractError, validate_ingest_payload
from vector_lake.schema_validator import category_shape_violation

REPO_ROOT = Path(__file__).resolve().parents[1]

MINIMAL_PURPOSE = """---
title: "Strategic Purpose Contract"
purpose_version: "12.1"
intent_keywords: ["HIT"]
intent_weight_boost: 0.0
scope:
  core: ["HIT"]
  edge: ["LLM"]
  excluded: ["Pure Science"]
  marketing_noise: ["Industry leading"]
evidence_tiers:
  "observed": "Seen directly."
sir_registry:
  - id: "SIR-1"
    status: "active"
    review_after: "2026-12-31"
    signal_keywords: ["HIS"]
synthesis_policy:
  min_distinct_sources: 2
  min_tension_intensity: 0.5
---
Purpose body.
"""


@pytest.fixture(autouse=True)
def _purpose_contract(isolated_memory):
    """``validate_ingest_payload`` reads the contract from the memory root, so provide one."""
    (isolated_memory / "purpose.md").write_text(MINIMAL_PURPOSE, encoding="utf-8")
    yield

FRONTMATTER = """---
id: {id}
title: "{title}"
aliases: []
type: concept
domain: Healthcare
topic_cluster: General
status: active
epistemic-status: verified
ttl: 365
memory_type: fact
memory_key: {id}
categories: {categories}
tags: []
strategic_scope: core
evidence_tier: observed
updated: 2026-09-19
sources: ["raw/x.md"]
---
Body text.
"""


def _item(categories: str, name: str = "Concept_Cats.md") -> dict:
    return {
        "filename": name,
        "content": FRONTMATTER.format(id="x1", title="Cats", categories=categories),
    }


def _validate(categories: str):
    return validate_ingest_payload([_item(categories)], contract=None)


def _shape(categories: str):
    return category_shape_violation(
        {"categories": _parse_categories(categories), "tags": []}, "Concept_Cats.md"
    )


def _parse_categories(categories: str):
    return yaml.safe_load(f"categories: {categories}\n")["categories"]


def test_the_validator_accepts_a_single_element_list():
    records = _validate('["Healthcare_IT"]')
    assert [record["filename"] for record in records] == ["Concept_Cats.md"]


@pytest.mark.parametrize(
    "categories,described",
    [
        ('["Healthcare_IT", "System_Architecture"]', "a list of 2 element(s)"),
        ("Healthcare_IT", "str"),
        ("[]", "a list of 0 element(s)"),
    ],
)
def test_a_bad_categories_shape_is_refused_with_what_arrived(categories, described):
    """The recorded reason is all an operator sees, so it has to name the offending value.

    The rule moved: it used to live in ``purpose_contract.validate_ingest_payload`` *and* in the
    new-node branch of ``schema_validator``, and between them they still let a bad shape through
    on an update in ``schema`` mode.  It is now ``category_shape_violation``, enforced on every
    write, so this asserts the owner instead of the gate that used to carry a copy.
    """
    message = _shape(categories)

    assert "categories must be a list with exactly one domain" in message
    assert described in message


def test_missing_categories_is_reported_as_missing():
    # A flow-style empty scalar parses as "", which is neither a list nor absent; the strict
    # case (field omitted entirely) is what "no value" describes, asserted separately.
    message = category_shape_violation({"categories": ""}, "Concept_None.md")
    assert "Received str" in message, message

    assert category_shape_violation({}, "Concept_None.md") == (
        "categories must be a list with exactly one domain. Received no value."
    )


def test_the_purpose_gate_no_longer_carries_its_own_copy_of_the_rule():
    """Guard on the merge: a payload with a bad shape passes this gate and is refused later.

    One owner, one check.  The gate that used to raise here answers to the purpose contract, not
    to the schema, and the write it precedes is refused by ``category_shape_violation``.
    """
    assert [record["filename"] for record in _validate("Healthcare_IT")] == ["Concept_Cats.md"]

    import inspect

    source = inspect.getsource(validate_ingest_payload)
    assert "exactly one domain" not in source


def test_both_prompts_state_the_rule_the_validator_enforces():
    """The prompt and the validator are two owners of one rule; they must not drift.

    The invariant asserted here is the one that was broken: the prompt has to contain the words
    the validator acts on -- a *list* with *exactly one* element -- and not merely "one of the
    domains".
    """
    from vector_lake.ingest_worker import _subagent_ingest_prompt

    template = (REPO_ROOT / "templates" / "ingest_prompt.md").read_text(encoding="utf-8")
    handoff = _subagent_ingest_prompt("base")

    for text, label in ((template, "templates/ingest_prompt.md"), (handoff, "handoff prompt")):
        lowered = text.lower()
        assert "exactly one element" in lowered, label
        assert "list" in lowered, label
    assert 'categories: ["Healthcare_IT"]' in template
    assert "categories" in handoff


def test_the_prompt_names_the_shapes_that_are_refused():
    """Spelling out the two wrong shapes is the part that was missing."""
    template = (REPO_ROOT / "templates" / "ingest_prompt.md").read_text(encoding="utf-8")
    assert "bare string" in template
    assert "multi-element list" in template


@pytest.fixture
def source(isolated_memory):
    db_store.init_db()
    raw = isolated_memory / "raw" / "news"
    raw.mkdir(parents=True, exist_ok=True)
    path = raw / "target.md"
    path.write_text("# a source\n", encoding="utf-8")
    return str(path)


def _make_dispatchable(conn, job_id: str) -> None:
    """Move the backoff out of the way so an assertion is about the cap, not the clock."""
    from datetime import datetime, timedelta, timezone

    with db_store.transaction():
        conn.execute(
            "UPDATE jobs SET available_at = ? WHERE job_id = ?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), job_id),
        )


def _job_for(source_path: str, job_id: str = "job-1") -> str:
    """A job for ``source_path`` carrying the hash the scan would compute.

    The abandonment key is ``(filepath, calculate_hash(file))`` -- the same value
    ``prepare_ingest_batch`` puts in the payload -- so a fixture with an invented hash would
    test nothing (and did: the skip silently did not fire).
    """
    conn = db_store.get_connection()
    payload = {"filepath": source_path, "hash": tool_ingest.calculate_hash(source_path),
               "canonical_name": "Source_target.md"}
    with db_store.transaction():
        conn.execute(
            "INSERT OR REPLACE INTO jobs (job_id, task_type, payload, status, retries, error_msg, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (job_id, "ingest", json.dumps(payload), "subagent_processing", 0, "",
             "2026-09-19T00:00:00+00:00", "2026-09-19T00:00:00+00:00"),
        )
    return job_id


def test_a_source_that_keeps_failing_is_abandoned(source):
    job_id = _job_for(source)
    messages = [
        tool_ingest.record_ingest_failure(job_id, "finalize rejected: Schema Violation: categories")
        for _ in range(db_store.MAX_INGEST_ATTEMPTS)
    ]

    assert "will be re-dispatched" in messages[0]
    assert "source abandoned" in messages[-1]
    assert db_store.abandoned_source_keys() == {(source, tool_ingest.calculate_hash(source))}
    listing = tool_ingest.list_abandoned_ingest_sources()
    assert "abandoned ingest source" in listing
    assert "categories" in listing


def test_an_abandoned_source_is_not_dispatched_again(source):
    """The whole point: the sweep stops spending model calls on content that keeps failing."""
    job_id = _job_for(source)
    for _ in range(db_store.MAX_INGEST_ATTEMPTS):
        tool_ingest.record_ingest_failure(job_id, "finalize rejected: Schema Violation: categories")

    message = tool_ingest.prepare_ingest_batch(batch_size=10)

    assert "abandoned" in message
    assert "No new files to ingest" in message
    queued = db_store.get_connection().execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'queued'"
    ).fetchone()[0]
    assert queued == 0, "an abandoned source was dispatched again"


def test_editing_the_source_makes_it_dispatchable_again(source):
    """The key is content, not path: fixing the file must not require an operator action."""
    job_id = _job_for(source)
    for _ in range(db_store.MAX_INGEST_ATTEMPTS):
        tool_ingest.record_ingest_failure(job_id, "finalize rejected: Schema Violation: categories")

    Path(source).write_text("# a source, now fixed\n", encoding="utf-8")
    message = tool_ingest.prepare_ingest_batch(batch_size=10)

    assert "enqueued 1" in message


def test_the_operator_can_clear_an_abandonment(source):
    job_id = _job_for(source)
    for _ in range(db_store.MAX_INGEST_ATTEMPTS):
        tool_ingest.record_ingest_failure(job_id, "finalize rejected: Schema Violation: categories")
    assert db_store.abandoned_source_keys()

    cleared = tool_ingest.clear_abandoned_ingest_sources(source)
    assert "Cleared 1" in cleared
    assert db_store.abandoned_source_keys() == set()

    assert "enqueued 1" in tool_ingest.prepare_ingest_batch(batch_size=10)


def test_a_transient_failure_never_abandons_the_source(source):
    """A version conflict is retryable, so it must not dead-letter the content."""
    job_id = _job_for(source)
    for _ in range(5):
        tool_ingest.record_ingest_failure(job_id, "finalize rejected: Canonical version conflict")

    assert db_store.abandoned_source_keys() == set()


def test_the_abandonment_records_each_terminal_job(source):
    for index in range(2):
        job_id = _job_for(source, f"job-{index}")
        for _ in range(db_store.MAX_INGEST_ATTEMPTS):
            tool_ingest.record_ingest_failure(job_id, "finalize rejected: Schema Violation")

    rows = db_store.list_abandoned_sources()
    assert len(rows) == 1
    assert rows[0]["terminal_jobs"] == 2


def test_the_scan_survives_a_database_without_the_table(source, monkeypatch):
    """A pre-migration snapshot must degrade to "no abandoned sources", not raise."""
    def _missing(*args, **kwargs):
        raise db_store.sqlite3.OperationalError("no such table: ingest_abandoned_sources")

    monkeypatch.setattr(db_store, "get_connection", lambda: type("C", (), {"execute": staticmethod(_missing)})())
    assert db_store.abandoned_source_keys() == set()
    assert db_store.list_abandoned_sources() == []


def test_a_finished_job_is_not_superseded_by_default(source):
    """Re-ingesting finished work would duplicate published pages."""
    conn = db_store.get_connection()
    payload = {"filepath": source, "hash": tool_ingest.calculate_hash(source),
               "canonical_name": "Source_target.md"}
    job_id = db_store.enqueue_job("ingest", payload)
    with db_store.transaction():
        conn.execute("UPDATE jobs SET status='finalized' WHERE job_id=?", (job_id,))

    assert db_store.enqueue_job("ingest", payload, replace_terminal=True) == job_id


def test_a_finished_job_is_superseded_for_an_unpublished_source(source, caplog):
    caplog.set_level(logging.WARNING, logger="vector-lake-db")
    """The scan's case: the job says finished, the source has no page.

    Measured on ``DHWB-20260913.md``: a finished job held the idempotency key while the source had
    neither a page nor a ledger row, so ``replace_terminal`` alone kept returning the finished id,
    the scan reported "enqueued", the file was marked in-flight, nothing was dispatched, and the
    next sweep released the marker and repeated -- every 12 minutes, forever.
    """
    conn = db_store.get_connection()
    payload = {"filepath": source, "hash": tool_ingest.calculate_hash(source),
               "canonical_name": "Source_target.md"}
    job_id = db_store.enqueue_job("ingest", payload)
    with db_store.transaction():
        # Only the status: overwriting the key would make the collision impossible and the
        # test would pass for the wrong reason.
        conn.execute("UPDATE jobs SET status='finalized' WHERE job_id=?", (job_id,))

    fresh = db_store.enqueue_job(
        "ingest", payload, replace_terminal=True, supersede_finished=True
    )

    assert fresh != job_id, "the finished job still holds the key"
    row = conn.execute("SELECT status, retries FROM jobs WHERE job_id=?", (fresh,)).fetchone()
    assert row["status"] == "queued"
    assert row["retries"] == 0
    # The collision is reported: page and ledger disagree, which an operator should see.
    assert any("the page and the ledger disagree" in record.message for record in caplog.records)


def test_the_scan_supersedes_a_finished_job_end_to_end(source):
    """After the fix, a scan on such a source produces a dispatchable job instead of a no-op."""
    conn = db_store.get_connection()
    payload = {"filepath": source, "hash": tool_ingest.calculate_hash(source),
               "canonical_name": "Source_target.md"}
    job_id = db_store.enqueue_job("ingest", payload)
    with db_store.transaction():
        conn.execute("UPDATE jobs SET status='finalized' WHERE job_id=?", (job_id,))

    message = tool_ingest.prepare_ingest_batch(batch_size=5)

    assert "enqueued 1" in message
    # Parse the payload rather than matching it with LIKE: the stored JSON escapes the Windows
    # separators, so a path pattern with single backslashes never matches.
    queued = [
        row[0]
        for row in conn.execute("SELECT payload FROM jobs WHERE status='queued'")
        if json.loads(row[0] or "{}").get("filepath") == source
    ]
    assert len(queued) == 1, "the scan did not create a dispatchable job"


def test_the_predicate_vocabulary_has_one_owner_and_reaches_the_prompts():
    """`Invalid predicate 'derived_from'` was a live failure, next to the `categories` shape.

    The vocabulary lives in ``schema_validator`` and both prompt surfaces receive it from there,
    so a model cannot be asked to guess it -- and adding a predicate cannot leave the prompts
    describing the old set.
    """
    from vector_lake import tool_ingest
    from vector_lake.ingest_worker import _subagent_ingest_prompt
    from vector_lake.schema_validator import VALID_PREDICATES

    template = (REPO_ROOT / "templates" / "ingest_prompt.md").read_text(encoding="utf-8")
    assert "{{valid_predicates}}" in template, "the template must not hardcode the vocabulary"

    rendered = tool_ingest._build_ingest_instructions(
        str(REPO_ROOT / "README.md"), "hash", "Source_x.md"
    )
    assert "{{valid_predicates}}" not in rendered
    assert "derived_from" not in VALID_PREDICATES
    for predicate in ("is-a", "part-of", "conflicts-with"):
        assert predicate in rendered, predicate
        assert predicate in _subagent_ingest_prompt("base"), predicate


def test_an_in_flight_job_is_not_superseded(isolated_memory, source):
    """The regression that produced three to four concurrent jobs for one source.

    ``replace_terminal`` asked "is it not finished", which is also true of ``queued``,
    ``awaiting_subagent`` and ``subagent_processing`` -- so a concurrent scan superseded live work
    instead of deduplicating against it.  Measured live 2026-09-19 10:50: six sources with three
    to four jobs each, i.e. as many model calls per file.
    """
    conn = db_store.get_connection()
    payload = {"filepath": source, "hash": tool_ingest.calculate_hash(source),
               "canonical_name": "Source_target.md"}
    first = db_store.enqueue_job("ingest", payload)

    for status in ("queued", "awaiting_subagent", "subagent_processing", "dispatched"):
        with db_store.transaction():
            conn.execute("UPDATE jobs SET status=? WHERE job_id=?", (status, first))
        assert db_store.enqueue_job("ingest", payload, replace_terminal=True) == first, status
        assert db_store.enqueue_job(
            "ingest", payload, replace_terminal=True, supersede_finished=True
        ) == first, status


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_a_given_up_job_is_still_superseded(isolated_memory, source, status):
    conn = db_store.get_connection()
    payload = {"filepath": source, "hash": tool_ingest.calculate_hash(source),
               "canonical_name": "Source_target.md"}
    first = db_store.enqueue_job("ingest", payload)
    with db_store.transaction():
        conn.execute("UPDATE jobs SET status=? WHERE job_id=?", (status, first))

    fresh = db_store.enqueue_job("ingest", payload, replace_terminal=True)
    assert fresh != first
    assert conn.execute("SELECT status FROM jobs WHERE job_id=?", (fresh,)).fetchone()[0] == "queued"


def test_superseding_a_failed_job_stops_it_being_dispatched(isolated_memory, source):
    """The superseded row must not stay claimable, or one source gets two dispatches.

    Clearing only the idempotency key left the old row ``failed`` with its retries unspent, and
    ``claim_pending_jobs`` claims ``failed`` while ``retries < MAX_INGEST_ATTEMPTS`` -- so both the
    old row and its replacement were handed out: two model calls for one source.  Found by the
    independent review of 2026-09-19, in the very path that was added to remove double dispatch.
    """
    conn = db_store.get_connection()
    payload = {"filepath": source, "hash": tool_ingest.calculate_hash(source),
               "canonical_name": "Source_target.md"}
    first = db_store.enqueue_job("ingest", payload)
    # One failure: terminal-unsuccessful but inside the budget, which is what re-dispatches.
    tool_ingest.record_ingest_failure(first, "finalize rejected: Schema Violation")
    _make_dispatchable(conn, first)

    second = db_store.enqueue_job("ingest", payload, replace_terminal=True)

    assert second != first
    old_status, old_key = conn.execute(
        "SELECT status, idempotency_key FROM jobs WHERE job_id = ?", (first,)
    ).fetchone()
    assert old_status == "superseded", old_status
    assert old_key is None

    _make_dispatchable(conn, second)
    claimed = [job["job_id"] for job in db_store.claim_pending_jobs(limit=5)]
    assert claimed == [second], f"the superseded row was dispatched too: {claimed}"


def test_superseding_a_finished_job_keeps_it_as_the_record(isolated_memory, source):
    """A finished row is already unclaimable, so its status stays as the record of the work."""
    conn = db_store.get_connection()
    payload = {"filepath": source, "hash": tool_ingest.calculate_hash(source),
               "canonical_name": "Source_target.md"}
    first = db_store.enqueue_job("ingest", payload)
    with db_store.transaction():
        conn.execute("UPDATE jobs SET status='finalized' WHERE job_id=?", (first,))

    db_store.enqueue_job("ingest", payload, replace_terminal=True, supersede_finished=True)

    assert conn.execute("SELECT status FROM jobs WHERE job_id=?", (first,)).fetchone()[0] == "finalized"
