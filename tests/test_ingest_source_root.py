"""Confinement of ingest source reads to the raw root.

A packet's ``filepath`` is data, not a capability.  Four call sites used to hand it straight
to ``open``, so a payload naming any readable file would have that file compiled into a
canonical wiki page.  These tests pin the boundary at the level where it is enforced (the
shared resolver) and at the level where it was exploitable (finalize).
"""

import json

import pytest

from vector_lake import db_store, mcp_server, wiki_utils
from vector_lake.tool_ingest import claim_ingest_tasks

from tests.test_mutation_coordinator import _write_purpose_contract


def _raw_source(isolated_memory, relative="news/x.md", text="# x\n"):
    path = isolated_memory / "raw" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _outside_file(isolated_memory, name="Secret.md", text="-----BEGIN PRIVATE KEY-----\nMIIabc\n"):
    """A readable file outside the raw tree (and outside the wiki, so a leak is detectable)."""
    path = isolated_memory / "outside" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_rooted_path_inside_the_raw_tree_resolves(isolated_memory):
    source = _raw_source(isolated_memory)
    assert wiki_utils.resolve_ingest_source_path(str(source)) == source.resolve()


def test_the_relative_spellings_that_packets_use_resolve(isolated_memory):
    """``raw/x.md`` is relative to the memory root and ``x.md`` to the raw root."""
    source = _raw_source(isolated_memory)
    assert wiki_utils.resolve_ingest_source_path("raw/news/x.md") == source.resolve()
    assert wiki_utils.resolve_ingest_source_path("news/x.md") == source.resolve()


def test_a_path_that_escapes_the_raw_root_resolves_to_nothing(isolated_memory):
    outside = _outside_file(isolated_memory)
    assert wiki_utils.resolve_ingest_source_path(str(outside)) is None
    # A sibling of the raw root reached by traversal, in both spellings.
    assert wiki_utils.resolve_ingest_source_path("../wiki/Secret.md") is None
    assert wiki_utils.resolve_ingest_source_path("raw/../wiki/Secret.md") is None
    # The planted file's own real path, reached the same way.
    assert wiki_utils.resolve_ingest_source_path(f"../{outside.parent.name}/{outside.name}") is None
    assert wiki_utils.resolve_ingest_source_path("") is None
    assert wiki_utils.resolve_ingest_source_path(None) is None


def test_an_absolute_path_outside_the_memory_tree_resolves_to_nothing(isolated_memory):
    """The check is membership in the raw root, not merely absence of traversal."""
    assert wiki_utils.resolve_ingest_source_path(str(isolated_memory.parent / "elsewhere.md")) is None


def test_the_owner_refuses_to_read_outside_the_raw_root(isolated_memory):
    outside = _outside_file(isolated_memory)
    assert wiki_utils.read_ingest_item_content({"filepath": str(outside)}) == ""
    with pytest.raises(ValueError, match="outside the raw root"):
        wiki_utils.read_ingest_item_content({"filepath": str(outside)}, required=True)


def test_the_owner_reads_once_and_caches_on_the_item(isolated_memory):
    source = _raw_source(isolated_memory)
    item = {"filepath": str(source)}
    assert wiki_utils.read_ingest_item_content(item) == "# x\n"
    assert item["content"] == "# x\n"
    source.unlink()
    assert wiki_utils.read_ingest_item_content(item) == "# x\n", "the cached read must survive"


def test_the_owner_reports_an_unreadable_source_the_way_each_caller_needs(isolated_memory):
    """``""`` for the best-effort callers, an exception for the ones that write a page."""
    missing = isolated_memory / "raw" / "news" / "gone.md"
    assert wiki_utils.read_ingest_item_content({"filepath": str(missing)}) == ""
    with pytest.raises(OSError):
        wiki_utils.read_ingest_item_content({"filepath": str(missing)}, required=True)


def test_finalize_ingest_refuses_a_payload_source_outside_the_raw_root(isolated_memory):
    """The exploit this exists for: a packet whose ``filepath`` is not a raw source.

    The read used to be a bare ``open``, so this payload published the contents of any file
    the ingest process could read.
    """
    _write_purpose_contract(isolated_memory)
    secret = _outside_file(isolated_memory)
    payload = {
        "filepath": str(secret),
        "hash": "secret-hash",
        "canonical_name": "Source_Leak.md",
    }
    db_store.init_db()
    job_id = db_store.enqueue_job("ingest", payload)
    db_store.mark_job_awaiting_subagent(job_id, "")
    claim = json.loads(claim_ingest_tasks(limit=1, lease_seconds=60))[0]

    result = mcp_server.tools.finalize_ingest(
        [{"filename": "Source_Leak.md", "filepath": str(secret)}],
        {
            **payload,
            "integration": {
                "disposition": "standalone",
                "reason": "Single-source compilation with no cross-page integration.",
            },
            "job_id": job_id,
            "lease_owner": claim["lease_owner"],
            "lease_token": claim["lease_token"],
            "lease_generation": claim["lease_generation"],
        },
    )

    assert "outside the raw root" in result, result
    assert not (isolated_memory / "wiki" / "Source_Leak.md").exists()
    written = "".join(
        page.read_text(encoding="utf-8") for page in (isolated_memory / "wiki").rglob("*.md")
    )
    assert "PRIVATE KEY" not in written, "the out-of-root file reached the wiki"
