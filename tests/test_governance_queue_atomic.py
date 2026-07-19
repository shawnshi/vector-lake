from concurrent.futures import ThreadPoolExecutor

from vector_lake import governance_service, governance_store


def _raise_full_queue_access(*_args, **_kwargs):
    raise AssertionError("full governance queue access is forbidden for row-level mutation")


def test_concurrent_enqueue_uses_row_level_writes(isolated_memory, monkeypatch):
    monkeypatch.setattr(governance_store, "load_governance_queue", _raise_full_queue_access)
    monkeypatch.setattr(governance_store, "save_governance_queue", _raise_full_queue_access)

    def enqueue(index):
        return governance_store.enqueue_governance_item(
            "suggestion",
            f"Candidate {index}",
            "Concurrent candidate",
            "test",
            [],
            [f"Concept_{index}.md"],
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        items = list(pool.map(enqueue, range(12)))

    rows = governance_store.get_connection().execute(
        "SELECT item_id, data_json FROM governance_queue ORDER BY item_id"
    ).fetchall()
    assert len(rows) == 12
    assert len({item["item_id"] for item in items}) == 12


def test_resolve_updates_one_row_without_overwriting_peer(isolated_memory, monkeypatch):
    target = governance_store.enqueue_governance_item(
        "suggestion", "Target", "Resolve me", "test", [], ["Concept_Target.md"]
    )
    peer = governance_store.enqueue_governance_item(
        "suggestion", "Peer", "Keep me", "test", [], ["Concept_Peer.md"]
    )
    monkeypatch.setattr(governance_store, "load_governance_queue", _raise_full_queue_access)
    monkeypatch.setattr(governance_store, "save_governance_queue", _raise_full_queue_access)

    resolved = governance_service.resolve_governance_item(target["item_id"], resolution="skip")

    assert resolved["status"] == "resolved"
    assert governance_store.get_governance_item(peer["item_id"])["status"] == "pending"
    assert governance_store.get_governance_item(target["item_id"])["resolution"] == "skip"


def test_legacy_queue_save_cannot_delete_rows_missing_from_snapshot(isolated_memory):
    left = governance_store.enqueue_governance_item(
        "suggestion", "Left", "Left", "test", [], ["Concept_Left.md"]
    )
    right = governance_store.enqueue_governance_item(
        "suggestion", "Right", "Right", "test", [], ["Concept_Right.md"]
    )

    governance_store.save_governance_queue({"items": [left]})

    assert governance_store.get_governance_item(left["item_id"]) is not None
    assert governance_store.get_governance_item(right["item_id"]) is not None
