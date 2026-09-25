"""The tantivy FTS backend: switch-gated, contract-compatible with the FTS5 path.

Every assertion here is about the *contract* ``db_store`` promises its callers, because that is
what a backend swap can silently break:

* default stays FTS5 -- nothing changes until an operator asks;
* the returned rows keep ``{node_key, title, summary, rank}`` with FTS5's **negative** rank
  (ascending = best first), which ``tool_search`` depends on when it does ``raw_score * -1.0``;
* terms are ANDed, each matching across title/summary/text;
* delete / clear / rebuild keep the mirror and the authoritative FTS5 tables in step.

No model, no network: text is passed pre-tokenized the way the indexer passes it.
"""
from __future__ import annotations

import pytest

from vector_lake import db_store, tantivy_index, tokenizer

pytest.importorskip("tantivy", reason="the tantivy wheel is not installed on this host")

DOCS = [
    ("Concept_信创医院", "信创 医院 HIS", "信创 集成平台 解耦", "信创 医院 HIS 集成平台 解耦 架构"),
    ("Concept_电子病历", "电子病历 六级 评级", "电子病历 评级 评审", "电子病历 六级 评级 评审 材料"),
    ("Concept_DRG医保", "DRG 医保 支付", "DRG DIP 合规", "DRG 医保 DIP 支付 合规 运营"),
]


def _seed(rows=DOCS):
    """Write the fixtures the way the indexer does: text already tokenized by jieba-rs.

    Feeding raw prose here would test an index nobody builds -- jieba segments ``解耦`` into
    ``解`` + ``耦`` and ``电子病历`` into ``电子`` + ``病历``, so an untokenized fixture only ever
    matches a whole-word query, which is the opposite of what ``search_wiki`` sends.
    """
    for key, title, summary, text in rows:
        db_store.upsert_search_index(
            key,
            tokenizer.tokenize_joined(title),
            tokenizer.tokenize_joined(summary),
            tokenizer.tokenize_joined(text),
        )


@pytest.fixture
def mirror(isolated_memory, monkeypatch):
    """A hermetic MEMORY root with the tantivy backend switched on."""
    db_store.init_db()
    monkeypatch.setenv("VECTOR_LAKE_FTS", "tantivy")
    tantivy_index.reset()  # drop handles cached against a previous test's root
    yield
    tantivy_index.reset()


def test_default_backend_is_fts5(isolated_memory, monkeypatch):
    monkeypatch.delenv("VECTOR_LAKE_FTS", raising=False)
    assert tantivy_index.enabled() is False
    assert tantivy_index.stats()["exists"] is False


def test_switch_is_honoured(mirror):
    assert tantivy_index.enabled() is True


def test_upsert_then_search_keeps_the_row_shape_and_the_rank_sign(mirror):
    _seed()

    rows = db_store.search_wiki("信创 医院", limit=10)

    assert [row["node_key"] for row in rows] == ["Concept_信创医院"]
    assert set(rows[0]) == {"node_key", "title", "summary", "rank"}
    assert rows[0]["rank"] < 0, "FTS5's bm25() is negative and tool_search negates it"


def test_terms_are_anded_across_fields(mirror):
    _seed()

    # '信创' is in the title, '解耦' only in the summary: both must be required.  The query goes
    # through the project tokenizer, so '解耦' arrives as '解' + '耦'.
    assert [row["node_key"] for row in db_store.search_wiki("信创 解耦", limit=10)] == [
        "Concept_信创医院"
    ]
    # A term nobody carries must empty the result rather than widen it.
    assert db_store.search_wiki("信创 不存在的词", limit=10) == []


def test_best_first_ordering_is_ascending_by_rank(mirror):
    _seed()

    rows = db_store.search_wiki("DRG 医保", limit=10)
    ranks = [row["rank"] for row in rows]
    assert ranks == sorted(ranks), "ascending rank is best-first, as ORDER BY rank was"


def test_delete_and_clear_keep_the_mirror_in_step(mirror):
    _seed()
    assert db_store.search_index_keys() == {key for key, *_ in DOCS}

    db_store.delete_search_index("Concept_DRG医保")
    assert "Concept_DRG医保" not in db_store.search_index_keys()
    assert db_store.search_wiki("DRG 医保", limit=10) == []

    db_store.clear_search_index()
    assert db_store.search_index_keys() == set()
    assert db_store.search_wiki("信创 医院", limit=10) == []


def test_rebuild_from_the_authoritative_fts_table(mirror):
    """The migration/recovery path: seed FTS5 only, then rebuild the mirror from it."""
    _seed()
    db_store.clear_search_index()  # wipes both
    conn = db_store.get_connection()
    for key, title, summary, text in DOCS:
        conn.execute(
            "INSERT INTO wiki_search_index (node_key, title, summary, text) VALUES (?, ?, ?, ?)",
            (
                key,
                tokenizer.tokenize_joined(title),
                tokenizer.tokenize_joined(summary),
                tokenizer.tokenize_joined(text),
            ),
        )
    conn.commit()
    tantivy_index.reset()

    assert tantivy_index.rebuild_from_sqlite() == len(DOCS)
    assert [row["node_key"] for row in db_store.search_wiki("电子病历 六级", limit=10)] == [
        "Concept_电子病历"
    ]


def test_a_mirror_failure_never_loses_the_authoritative_write(mirror, monkeypatch):
    """Fail-open: the FTS5 row is the write; the mirror is an optimisation."""
    def boom(*_args, **_kwargs):
        raise RuntimeError("mirror down")

    monkeypatch.setattr(tantivy_index, "upsert", boom)
    db_store.upsert_search_index("Concept_A", "标题", "摘要", "文本")

    conn = db_store.get_connection()
    row = conn.execute(
        "SELECT node_key FROM wiki_search_index WHERE node_key = 'Concept_A'"
    ).fetchone()
    assert row is not None, "the authoritative projection must still have the node"
