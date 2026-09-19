"""A published source with no ledger row: the ledger must catch up, without retiring an edit.

`_already_published`'s own docstring describes the state -- "a source ingested while the ingest
pipeline was stalled has a page but no row" -- and the skip it induces has a second consequence it
does not mention: the published branch runs *before* the hash comparison a ledger row enables, so
**a later edit to such a source is never ingested**.  Measured on 2026-09-19: exactly one source in
that state (`raw/医疗信息化/推动健康中国建设取得决定性进展有关情况.md`, page created 2026-09-14, raw
mtime 2026-09-14), and it was the only thing keeping the un-ingested count from reaching zero.

The reconciliation writes the row only when the raw content cannot have changed since the page was
written.  Two kinds of evidence, in order of strength: a `source_hash` stamped on the page by
`finalize_ingest` (proof), or -- for the pages that predate the stamp -- the page's `created` date
against the file's mtime.  When the evidence says the file changed, the row is refused and the
source stays pending, so the edit is ingested rather than retired.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from vector_lake import db_store, tool_ingest
from vector_lake.wiki_utils import get_wiki_dir, split_frontmatter

PAGE = """---
id: "20260914_aaaaaaaaaaaa"
title: "Source demo"
type: "source"
status: "Active"
categories: ["Uncategorized"]
sources: ["raw/news/target.md"]
created: "{created}"
---
Body.
"""


@pytest.fixture
def published_source(isolated_memory):
    """A raw source that a page declares, with no ``processed_files`` row."""
    raw = isolated_memory / "raw" / "news"
    raw.mkdir(parents=True, exist_ok=True)
    path = raw / "target.md"
    path.write_text("# a source\n", encoding="utf-8")
    # The scan builds ingest instructions, which refuse to run with a populated wiki and no
    # index projection (that is how duplicate entities get created); an empty node set is the
    # supported "cold knowledge base" shape.
    (isolated_memory / "wiki" / "index.json").write_text(
        json.dumps({"nodes": {}}), encoding="utf-8"
    )
    db_store.init_db()
    return str(path)


def _declare(path: str, created: str) -> Path:
    page = Path(str(get_wiki_dir())) / "Source_news-target-deadbeef.md"
    page.write_text(PAGE.format(created=created), encoding="utf-8")
    return page


def _set_mtime(path: str, iso_date: str) -> None:
    stamp = time.mktime(time.strptime(iso_date, "%Y-%m-%d"))
    os.utime(path, (stamp, stamp))


def _ledger_has(path: str) -> bool:
    return bool(
        db_store.get_connection()
        .execute("SELECT 1 FROM processed_files WHERE filepath = ?", (path,))
        .fetchone()
    )


def test_the_index_carries_the_page_its_date_and_any_stamped_hash(published_source):
    _declare(published_source, "2026-09-14")

    index = tool_ingest.raw_publication_index()

    assert tool_ingest._raw_key(published_source) in index["declared_keys"]
    entry = index["declared_pages"][tool_ingest._raw_key(published_source)]
    assert entry["page"] == "Source_news-target-deadbeef.md"
    assert entry["created"] == "2026-09-14"
    assert entry["source_hash"] == "", "a legacy page records no hash"


def test_an_unchanged_published_source_gets_its_ledger_row(published_source):
    _declare(published_source, "2026-09-14")
    _set_mtime(published_source, "2026-09-14")
    assert tool_ingest._published_source_verdict(
        published_source, tool_ingest.raw_publication_index()
    ) == "record"

    message = tool_ingest.prepare_ingest_batch(batch_size=5)

    assert _ledger_has(published_source), "the missing ledger row was not recorded"
    assert "Recorded 1 already-published source" in message
    stored = db_store.get_connection().execute(
        "SELECT file_hash FROM processed_files WHERE filepath = ?", (published_source,)
    ).fetchone()[0]
    assert stored == tool_ingest.calculate_hash(published_source)


def test_an_edited_published_source_is_not_retired(published_source):
    """The freeze this fixes: the row must NOT be written when the file changed after publication."""
    _declare(published_source, "2026-09-14")
    _set_mtime(published_source, "2026-09-18")
    assert tool_ingest._published_source_verdict(
        published_source, tool_ingest.raw_publication_index()
    ) == "stale"

    message = tool_ingest.prepare_ingest_batch(batch_size=5)

    assert not _ledger_has(published_source), "an edit was retired instead of ingested"
    assert "Recorded" not in message
    statuses = [
        row[0] for row in db_store.get_connection().execute("SELECT status FROM jobs")
    ]
    assert "queued" in statuses, "the edited source was not dispatched"


def test_a_ledger_row_makes_a_future_edit_visible(published_source):
    """Why recording the row matters: it is what enables the hash comparison."""
    _declare(published_source, "2026-09-14")
    _set_mtime(published_source, "2026-09-14")
    tool_ingest.prepare_ingest_batch(batch_size=5)
    assert _ledger_has(published_source)

    Path(published_source).write_text("# a source, revised\n", encoding="utf-8")
    message = tool_ingest.prepare_ingest_batch(batch_size=5)

    assert "enqueued 1" in message, "the edit was not detected once the row existed"


def test_a_name_signal_only_match_is_never_recorded(published_source):
    """The name signal is over-eager by design, so it may not retire a source permanently."""
    page = Path(str(get_wiki_dir())) / "Source_news-target-other.md"
    page.write_text(
        PAGE.format(created="2026-09-14").replace(
            'sources: ["raw/news/target.md"]', "sources: []"
        ),
        encoding="utf-8",
    )
    _set_mtime(published_source, "2026-09-14")

    index = tool_ingest.raw_publication_index()
    assert tool_ingest.raw_is_published(published_source, index) is True
    assert tool_ingest._published_source_verdict(published_source, index) == "skip"


@pytest.mark.parametrize("created", ["", "not-a-date", "2026/09/14"])
def test_an_unparsable_created_date_is_refused(published_source, created):
    _declare(published_source, created)
    index = tool_ingest.raw_publication_index()
    assert tool_ingest._published_source_verdict(published_source, index) == "skip"


def test_a_stamped_hash_decides_without_the_date(published_source):
    """The durable fix: a recorded hash is proof, so the mtime heuristic is not consulted.

    A page written with the raw hash it was compiled from answers "did this change?" exactly,
    which is what the date fallback can only infer.
    """
    page = _declare(published_source, "2026-09-14")
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            'created: "2026-09-14"',
            'created: "2026-09-14"\nsource_hash: "'
            + tool_ingest.calculate_hash(published_source)
            + '"',
        ),
        encoding="utf-8",
    )
    index = tool_ingest.raw_publication_index()
    assert index["declared_pages"][tool_ingest._raw_key(published_source)]["source_hash"]

    # Even with the file's mtime moved far into the future, the matching hash says "record".
    _set_mtime(published_source, "2099-01-01")
    assert tool_ingest._published_source_verdict(published_source, index) == "record"

    # ...and a changed file is detected without relying on any date at all.
    Path(published_source).write_text("# edited\n", encoding="utf-8")
    assert tool_ingest._published_source_verdict(published_source, index) == "stale"


def test_stamping_preserves_everything_else_about_the_page(isolated_memory):
    content = (
        "---\n"
        'id: "x"\n'
        'title: "t"\n'
        'created: "2026-09-14"\n'
        'sources: ["raw/news/target.md"]\n'
        "---\n"
        "Body.\n"
    )

    stamped = tool_ingest._stamp_source_hash(content, "deadbeef")

    frontmatter, body = split_frontmatter(stamped)
    assert frontmatter["source_hash"] == "deadbeef"
    assert frontmatter["created"] == "2026-09-14"
    assert frontmatter["sources"] == ["raw/news/target.md"]
    assert body == "Body.\n"
    # Idempotent, and a page without frontmatter is left alone rather than guessed at.
    assert tool_ingest._stamp_source_hash(stamped, "deadbeef") == stamped
    assert tool_ingest._stamp_source_hash("plain", "deadbeef") == "plain"
    assert tool_ingest._stamp_source_hash(stamped, "") == stamped
