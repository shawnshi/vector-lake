"""Contract tests for the two-stage (bit prefilter + exact rerank) vector index.

The parity claim for the production corpus is measured (24/24 queries, top-10 identical, 8.4 ms vs
234.9 ms).  These tests cover the *contract* around that claim in a closed, synthetic index: the
shadow must follow writes, drift must make it unavailable rather than wrong, and the two-stage
ranking must agree with the brute-force scan it replaces.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from vector_lake import db_store, tool_search, two_stage_index

DIM = 3072
DB_STORE = Path(db_store.__file__)


def _seed(monkeypatch, vectors: dict[str, np.ndarray]) -> None:
    """A closed index with a recorded projection, the given vectors and a built shadow."""
    db_store.init_db()
    db_store.record_embedding_projection("test-embedding", DIM)
    for key, vec in vectors.items():
        db_store.upsert_embedding(key, [float(x) for x in vec])
    conn = db_store.get_connection()
    with db_store.transaction():
        two_stage_index.build(conn)


def _corpus(rng: np.random.Generator, n: int, spread: float = 0.2) -> dict[str, np.ndarray]:
    """A retrieval-realistic population: all vectors near one base direction.

    Independent high-dimensional vectors are mutually orthogonal (cosine ~ 0), so they fall under
    the product's similarity gate and a top-10 comparison would be vacuous.  Perturbing a base
    keeps pairwise similarity near 0.96, so a real top-10 exists to compare.
    """
    base = rng.normal(size=DIM)
    base /= np.linalg.norm(base)
    scale = spread / DIM**0.5
    out = {}
    for i in range(n):
        vec = base + rng.normal(scale=scale, size=DIM)
        out[f"Concept_{i:03d}"] = vec / np.linalg.norm(vec)
    return out


def _unit(rng: np.random.Generator, n: int) -> dict[str, np.ndarray]:
    out = {}
    for i in range(n):
        vec = rng.normal(size=DIM)
        out[f"Concept_{i:03d}"] = vec / np.linalg.norm(vec)
    return out


def test_shadow_follows_upsert_and_delete(monkeypatch):
    rng = np.random.default_rng(11)
    vectors = _unit(rng, 3)
    _seed(monkeypatch, vectors)

    conn = db_store.get_connection()
    state = two_stage_index.status(conn)
    assert state is not None and state["row_count"] == 3

    late = rng.normal(size=DIM)
    db_store.upsert_embedding("Concept_late", [float(x) for x in (late / np.linalg.norm(late))])

    assert two_stage_index.status(conn) is not None, "an overwrite through upsert must keep counts"
    assert (
        conn.execute(
            f"SELECT COUNT(*) FROM {two_stage_index.FLOAT_TABLE} WHERE page_key = ?", ("Concept_late",)
        ).fetchone()[0]
        == 1
    ), "the mirror must write the new vector, not just the live table"
    assert (
        conn.execute(
            f"SELECT COUNT(*) FROM {two_stage_index.BITS_TABLE} WHERE page_key = ?", ("Concept_late",)
        ).fetchone()[0]
        == 1
    )

    db_store.delete_embedding("Concept_late")
    assert two_stage_index.status(conn) is not None
    assert (
        conn.execute(
            f"SELECT COUNT(*) FROM {two_stage_index.FLOAT_TABLE} WHERE page_key = ?", ("Concept_late",)
        ).fetchone()[0]
        == 0
    ), "a deleted vector must not survive in the shadow"


def test_drift_makes_the_shadow_unavailable_not_wrong(monkeypatch):
    rng = np.random.default_rng(12)
    _seed(monkeypatch, _unit(rng, 3))

    conn = db_store.get_connection()
    assert two_stage_index.status(conn) is not None

    # A writer that bypasses the mirror is exactly the failure this guard exists for.
    blob = np.zeros(DIM, dtype=np.float32).tobytes()
    with db_store.transaction():
        conn.execute("INSERT INTO vec_embeddings (page_key, embedding) VALUES (?, ?)", ("Concept_rogue", blob))

    assert two_stage_index.status(conn) is None, (
        "a shadow whose membership no longer matches the live table must be unavailable"
    )
    results, error = tool_search._get_vector_search_results([0.0] * DIM, limit=5)
    assert error is None, "the fallback path must still answer"


def test_two_stage_rank_matches_bruteforce(monkeypatch):
    rng = np.random.default_rng(20260929)
    vectors = _corpus(rng, 200)
    _seed(monkeypatch, vectors)

    # A random high-dimensional query is nearly orthogonal to every stored vector, so it can fall
    # below the product's similarity gate and return nothing at all.  Query with a stored vector
    # plus small noise: the target must be the top hit, which is what parity is measured on.
    target = vectors["Concept_042"]
    noisy = target + rng.normal(scale=1e-3, size=DIM)
    query = noisy / np.linalg.norm(noisy)
    query_list = [float(x) for x in query]

    monkeypatch.setenv("VECTOR_LAKE_VECTOR_INDEX", "two_stage")
    staged, error = tool_search._get_vector_search_results(query_list, limit=10)
    assert error is None and staged

    monkeypatch.setenv("VECTOR_LAKE_VECTOR_INDEX", "legacy")
    brute, error = tool_search._get_vector_search_results(query_list, limit=10)
    assert error is None and brute

    top_staged, top_brute = list(staged)[:10], list(brute)[:10]
    assert top_staged[0] == "Concept_042" and top_brute[0] == "Concept_042"
    overlap = len(set(top_staged) & set(top_brute)) / 10
    # The production parity claim is 24/24 identical top-10; this synthetic index is much smaller,
    # so the assertion allows a small shortlist-boundary difference instead of encoding flakiness.
    assert overlap >= 0.9, f"two-stage diverged from the exact scan: {top_staged} vs {top_brute}"


def test_shortlist_boundary_is_reported_by_status(monkeypatch):
    """``status`` is the read path's only trust input: it must expose the recorded shortlist."""
    rng = np.random.default_rng(7)
    _seed(monkeypatch, _unit(rng, 5))
    state = two_stage_index.status(db_store.get_connection())
    assert state is not None
    assert state["shortlist"] == two_stage_index.DEFAULT_SHORTLIST
    assert state["dimension"] == DIM
    assert state["model"] == "test-embedding"


def test_vec_embeddings_is_written_only_through_the_mirrored_helpers():
    """A second writer would keep row counts equal while serving stale vectors.

    The count check in ``status`` cannot see that, so the structural guarantee is asserted here:
    every raw write to the live table lives inside the two helpers that also mirror the shadow.
    """
    source = DB_STORE.read_text(encoding="utf-8")
    lines = source.splitlines()
    write_lines = [
        i
        for i, line in enumerate(lines)
        if re.search(r"(INSERT INTO vec_embeddings|DELETE FROM vec_embeddings)", line)
    ]
    assert write_lines, "the scan should find the write statements"

    def body(name: str) -> set[int]:
        start = next(i for i, line in enumerate(lines) if line.startswith(f"def {name}("))
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].startswith("def ") or lines[i].startswith("class ")),
            len(lines),
        )
        return set(range(start, end))

    allowed = body("_store_vector") | body("_delete_vectors")
    strays = [lines[i].strip() for i in write_lines if i not in allowed]
    assert strays == [], (
        "these writes bypass the mirrored helpers and can desynchronise the shadow index: "
        f"{strays}"
    )


def test_projection_change_invalidates_the_shadow(monkeypatch):
    rng = np.random.default_rng(21)
    _seed(monkeypatch, _unit(rng, 4))
    conn = db_store.get_connection()
    assert two_stage_index.status(conn) is not None

    db_store.record_embedding_projection("test-embedding", DIM)
    assert two_stage_index.status(conn) is not None, "recording the same projection must not invalidate"

    db_store.record_embedding_projection("some-other-encoder", DIM)
    assert two_stage_index.status(conn) is None, (
        "a projection change must disable the shadow until it is rebuilt, not mix encoders"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
