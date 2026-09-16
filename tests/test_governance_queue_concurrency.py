"""The governance queue is persisted by key-diff replacement.

``_save_db_queue`` deletes every key absent from the caller's snapshot, so a
writer that saves a snapshot taken before another writer appended *deletes* that
writer's item.  These tests pin the invariant that a whole load -> mutate -> save
cycle is serialised, and demonstrate the defect deterministically.
"""
import threading

from vector_lake import db_store, governance_store


def _item(item_id: str, source: str = "concurrency-test") -> dict:
    return {
        "item_id": item_id,
        "type": "research",
        "title": item_id,
        "description": "",
        "created_at": governance_store._utc_now(),
        "status": "pending",
        "source": source,
        "search_queries": [],
        "affected_pages": [],
    }


def _load_and_append(item_id: str, source: str = "concurrency-test"):
    """Unlocked (pre-fix) load -> append -> save, staged across two calls."""
    queue = governance_store.load_governance_queue()
    queue.setdefault("items", []).append(_item(item_id, source))
    return queue


def test_unlocked_snapshot_save_drops_a_concurrent_item(isolated_memory):
    """Deterministic interleaving proof of the lost update.

    Two writers interleave load -> append -> save.  The second save is taken from
    a snapshot that predates the first append, so it deletes the first item.
    """
    db_store.init_db()

    writer_a = _load_and_append("gov_a")
    writer_b = _load_and_append("gov_b")

    governance_store.save_governance_queue(writer_a)
    governance_store.save_governance_queue(writer_b)

    surviving = {item["item_id"] for item in governance_store.load_governance_queue()["items"]}
    assert "gov_b" in surviving
    assert "gov_a" not in surviving, (
        "if this ever passes, the key-diff save no longer drops concurrent items "
        "and the queue lock rationale must be revisited"
    )


def test_locked_enqueue_survives_concurrent_writers(isolated_memory):
    db_store.init_db()
    count, width = 8, 4
    barrier = threading.Barrier(width)

    def run(thread_index: int):
        barrier.wait()
        for offset in range(count):
            governance_store.enqueue_governance_item(
                "research", f"item {thread_index}-{offset}", "", f"concurrency-test-{thread_index}", [], []
            )

    threads = [threading.Thread(target=run, args=(index,)) for index in range(width)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    items = governance_store.load_governance_queue()["items"]
    titles = {item["title"] for item in items}
    expected = {f"item {t}-{o}" for t in range(width) for o in range(count)}
    assert expected.issubset(titles), f"lost queue items: {sorted(expected - titles)}"


def test_locked_batch_enqueue_is_atomic(isolated_memory):
    db_store.init_db()
    governance_store.enqueue_governance_items([_item("gov_1"), _item("gov_2")])
    assert governance_store.enqueue_governance_items([]) == 0
    surviving = {item["item_id"] for item in governance_store.load_governance_queue()["items"]}
    assert surviving == {"gov_1", "gov_2"}


def test_governance_queue_session_is_reentrant(isolated_memory):
    with governance_store.governance_queue_session():
        with governance_store.governance_queue_session():
            governance_store.enqueue_governance_item("research", "nested", "", "test", [], [])
    assert len(governance_store.load_governance_queue()["items"]) == 1
