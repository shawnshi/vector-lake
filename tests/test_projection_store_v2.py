from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import random
import statistics

import pytest

from vector_lake import durability
from vector_lake import projection_store_v2 as store_v2


def _store(isolated_memory: Path, monkeypatch) -> store_v2.ProjectionStoreV2:
    monkeypatch.setenv("VECTOR_LAKE_DURABILITY_PROFILE", "best_effort")
    return store_v2.ProjectionStoreV2(isolated_memory)


@pytest.mark.parametrize("count", [1, 10, 100])
def test_round_trip_uses_canonical_content_addressed_objects(
    isolated_memory,
    monkeypatch,
    count,
):
    store = _store(isolated_memory, monkeypatch)
    values = {
        f"key-{index:04d}": {"index": index, "text": f"value-{index}"}
        for index in range(count)
    }

    result = store.apply(store.empty_root_digest, sets=values)

    assert result.root_digest != store.empty_root_digest
    assert result.new_objects >= 1
    assert result.new_bytes > 0
    assert store.get(result.root_digest, "key-0000") == values["key-0000"]
    assert dict(store.iter_items(result.root_digest, limit=count + 1)) == values
    for path in store.object_paths(result.root_digest, max_objects=256):
        assert path.relative_to(isolated_memory).parts[:4] == (
            ".projection-store",
            "objects",
            "sha256",
            path.stem[:2],
        )
        payload = path.read_bytes()
        assert len(payload) <= store_v2.MAX_OBJECT_BYTES
        assert hashlib.sha256(payload).hexdigest() == path.stem
        assert payload == store_v2.canonical_json_bytes(json.loads(payload))


def test_empty_root_is_stable_virtual_canonical_leaf(isolated_memory, monkeypatch):
    first = _store(isolated_memory, monkeypatch)
    second = store_v2.ProjectionStoreV2(isolated_memory)

    assert first.empty_root_digest == second.empty_root_digest
    assert first.empty_root_digest == hashlib.sha256(
        store_v2.canonical_json_bytes(
            {"entries": [], "kind": "leaf", "version": 2}
        )
    ).hexdigest()
    assert first.iter_items(first.empty_root_digest, limit=1) == ()
    with pytest.raises(KeyError):
        first.get(first.empty_root_digest, "missing")


def test_repeat_batch_is_a_noop_with_zero_new_objects(isolated_memory, monkeypatch):
    store = _store(isolated_memory, monkeypatch)
    values = {f"key-{index}": {"value": index} for index in range(100)}
    first = store.apply(store.empty_root_digest, sets=values)

    repeated = store.apply(first.root_digest, sets=values)

    assert repeated.root_digest == first.root_digest
    assert repeated.new_objects == 0
    assert repeated.new_bytes == 0
    assert repeated.reused_objects >= 1
    assert repeated.reused_bytes > 0


def test_batch_set_delete_get_iteration_and_bounded_diff(isolated_memory, monkeypatch):
    store = _store(isolated_memory, monkeypatch)
    original = store.apply(
        store.empty_root_digest,
        sets={f"key-{index:03d}": index for index in range(80)},
    )

    changed = store.apply(
        original.root_digest,
        sets={"key-005": "changed", "new-key": {"ok": True}},
        deletes={"key-006", "key-079", "absent"},
    )

    assert store.get(changed.root_digest, "key-005") == "changed"
    assert store.get(changed.root_digest, "new-key") == {"ok": True}
    with pytest.raises(KeyError):
        store.get(changed.root_digest, "key-006")
    page_one = store.iter_items(changed.root_digest, limit=9)
    page_two = store.iter_items(
        changed.root_digest,
        limit=9,
        start_after=page_one[-1][0],
    )
    combined = page_one + page_two
    ordering = [(store_v2.key_digest(key), key) for key, _value in combined]
    assert ordering == sorted(ordering)
    assert not set(dict(page_one)).intersection(dict(page_two))

    difference = store.diff(original.root_digest, changed.root_digest, limit=10)
    assert not difference.truncated
    by_key = {item.key: item for item in difference.entries}
    assert set(by_key) == {"key-005", "key-006", "key-079", "new-key"}
    assert by_key["key-006"].left_exists
    assert not by_key["key-006"].right_exists
    assert not by_key["new-key"].left_exists
    assert by_key["new-key"].right_exists

    truncated = store.diff(original.root_digest, changed.root_digest, limit=2)
    assert truncated.truncated
    assert len(truncated.entries) == 2


def test_randomized_batches_match_reference_and_delete_to_stable_empty_root(
    isolated_memory,
    monkeypatch,
):
    store = _store(isolated_memory, monkeypatch)
    generator = random.Random(20260828)
    expected: dict[str, object] = {}
    root = store.empty_root_digest
    universe = [f"key-{index:04d}" for index in range(2_000)]

    for revision in range(24):
        set_keys = set(generator.sample(universe, 70))
        delete_keys = set(generator.sample(universe, 45)).difference(set_keys)
        values = {
            key: {"revision": revision, "value": generator.randrange(1_000_000)}
            for key in set_keys
        }
        result = store.apply(root, sets=values, deletes=delete_keys)
        expected.update(values)
        for key in delete_keys:
            expected.pop(key, None)
        root = result.root_digest
        assert dict(store.iter_items(root, limit=3_000)) == expected

    cleared = store.apply(root, deletes=expected)
    assert cleared.root_digest == store.empty_root_digest
    assert store.iter_items(cleared.root_digest, limit=1) == ()


def test_hash_tamper_and_noncanonical_or_invalid_shape_fail_closed(
    isolated_memory,
    monkeypatch,
):
    store = _store(isolated_memory, monkeypatch)
    committed = store.apply(store.empty_root_digest, sets={"safe": {"v": 1}})
    root_path = store.object_path(committed.root_digest)
    root_path.write_bytes(b"{}")

    with pytest.raises(store_v2.ProjectionIntegrityError, match="hash_mismatch"):
        store.get(committed.root_digest, "safe")

    bad_shape = store_v2.canonical_json_bytes(
        {"entries": [["wrong", 1], ["wrong", 2]], "kind": "leaf", "version": 2}
    )
    bad_digest = hashlib.sha256(bad_shape).hexdigest()
    bad_path = store.object_path(bad_digest)
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    bad_path.write_bytes(bad_shape)
    with pytest.raises(store_v2.ProjectionIntegrityError, match="leaf_key_order"):
        store.iter_items(bad_digest, limit=3)

    noncanonical = b'{"version":2, "kind":"leaf", "entries":[]}'
    noncanonical_digest = hashlib.sha256(noncanonical).hexdigest()
    noncanonical_path = store.object_path(noncanonical_digest)
    noncanonical_path.parent.mkdir(parents=True, exist_ok=True)
    noncanonical_path.write_bytes(noncanonical)
    with pytest.raises(store_v2.ProjectionIntegrityError, match="noncanonical"):
        store.iter_items(noncanonical_digest, limit=3)


def test_oversize_value_depth_limit_and_invalid_roots_fail_closed(
    isolated_memory,
    monkeypatch,
):
    store = _store(isolated_memory, monkeypatch)

    with pytest.raises(store_v2.ProjectionObjectLimitError, match="object_bytes"):
        store.apply(
            store.empty_root_digest,
            sets={"huge": "x" * store_v2.MAX_OBJECT_BYTES},
        )
    with pytest.raises(store_v2.ProjectionIntegrityError, match="digest"):
        store.get("../outside", "key")

    monkeypatch.setattr(store_v2, "key_digest", lambda _key: "0" * 64)
    with pytest.raises(store_v2.ProjectionDepthError, match="depth"):
        store.apply(
            store.empty_root_digest,
            sets={f"collision-{index}": index for index in range(257)},
        )


def test_singleton_leaf_uses_object_bound_without_raising_multi_leaf_bound(
    isolated_memory,
    monkeypatch,
):
    store = _store(isolated_memory, monkeypatch)
    value = "x" * (store_v2.MAX_LEAF_BYTES + 1024)

    committed = store.apply(store.empty_root_digest, sets={"large": value})

    assert store.get(committed.root_digest, "large") == value
    payload_size = store.object_path(committed.root_digest).stat().st_size
    assert store_v2.MAX_LEAF_BYTES < payload_size <= store_v2.MAX_OBJECT_BYTES


def test_singleton_leaf_accepts_exact_object_boundary_and_rejects_next_byte(
    isolated_memory,
    monkeypatch,
):
    store = _store(isolated_memory, monkeypatch)
    empty_payload = store_v2.canonical_json_bytes(
        {
            "entries": [["exact", ""]],
            "kind": "leaf",
            "version": store_v2.FORMAT_VERSION,
        }
    )
    value = "x" * (store_v2.MAX_OBJECT_BYTES - len(empty_payload))
    exact_payload = store_v2.canonical_json_bytes(
        {
            "entries": [["exact", value]],
            "kind": "leaf",
            "version": store_v2.FORMAT_VERSION,
        }
    )
    assert len(exact_payload) == store_v2.MAX_OBJECT_BYTES

    committed = store.apply(store.empty_root_digest, sets={"exact": value})
    assert store.object_path(committed.root_digest).stat().st_size == (
        store_v2.MAX_OBJECT_BYTES
    )
    assert store.get(committed.root_digest, "exact") == value

    with pytest.raises(store_v2.ProjectionObjectLimitError, match="object_bytes"):
        store.apply(
            store.empty_root_digest,
            sets={"exact": value + "x"},
        )


def test_large_values_split_into_bounded_singleton_leaves(
    isolated_memory,
    monkeypatch,
):
    store = _store(isolated_memory, monkeypatch)
    value = "x" * (store_v2.MAX_LEAF_BYTES // 2 + 1024)

    committed = store.apply(
        store.empty_root_digest,
        sets={"large-left": value, "large-right": value},
    )

    assert store.get(committed.root_digest, "large-left") == value
    assert store.get(committed.root_digest, "large-right") == value
    leaf_count = 0
    for path in store.object_paths(committed.root_digest, max_objects=64):
        payload = path.read_bytes()
        node = json.loads(payload)
        if node["kind"] != "leaf":
            continue
        leaf_count += 1
        if len(node["entries"]) == 1:
            assert len(payload) <= store_v2.MAX_OBJECT_BYTES
        else:
            assert len(payload) <= store_v2.MAX_LEAF_BYTES
    assert leaf_count == 2


def test_oversize_multi_entry_leaf_still_fails_closed(
    isolated_memory,
    monkeypatch,
):
    store = _store(isolated_memory, monkeypatch)
    payload = store_v2.canonical_json_bytes(
        {
            "entries": [
                ["left", "x" * (store_v2.MAX_LEAF_BYTES // 2)],
                ["right", "y" * (store_v2.MAX_LEAF_BYTES // 2)],
            ],
            "kind": "leaf",
            "version": store_v2.FORMAT_VERSION,
        }
    )
    assert store_v2.MAX_LEAF_BYTES < len(payload) < store_v2.MAX_OBJECT_BYTES
    digest = hashlib.sha256(payload).hexdigest()
    path = store.object_path(digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)

    with pytest.raises(store_v2.ProjectionObjectLimitError, match="leaf_bytes"):
        store.iter_items(digest, limit=3)


def test_oversize_stored_object_and_redirected_store_fail_closed(
    isolated_memory,
    monkeypatch,
):
    store = _store(isolated_memory, monkeypatch)
    oversized = b"x" * (store_v2.MAX_OBJECT_BYTES + 1)
    digest = hashlib.sha256(oversized).hexdigest()
    path = store.object_path(digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(oversized)
    with pytest.raises(store_v2.ProjectionObjectLimitError, match="object_bytes"):
        store.get(digest, "key")

    redirected_base = isolated_memory / "redirected"
    redirected_base.mkdir()
    outside = isolated_memory / "outside-store"
    outside.mkdir()
    try:
        os.symlink(
            outside,
            redirected_base / ".projection-store",
            target_is_directory=True,
        )
    except OSError:
        pytest.skip("directory symlinks are unavailable in this test context")
    redirected = store_v2.ProjectionStoreV2(redirected_base)
    with pytest.raises(store_v2.ProjectionSecurityError, match="redirect"):
        redirected.apply(redirected.empty_root_digest, sets={"key": "value"})


def test_crash_before_promotion_never_publishes_partial_object(
    isolated_memory,
    monkeypatch,
):
    store = _store(isolated_memory, monkeypatch)
    original_promote = store._promote_object

    def crash(_temporary, _target, _payload):
        raise OSError("injected pre-promotion crash")

    monkeypatch.setattr(store, "_promote_object", crash)
    with pytest.raises(OSError, match="pre-promotion"):
        store.apply(store.empty_root_digest, sets={"key": {"value": 1}})

    assert store.iter_items(store.empty_root_digest, limit=1) == ()
    assert not list((isolated_memory / ".projection-store").rglob("*.tmp-*"))

    monkeypatch.setattr(store, "_promote_object", original_promote)
    committed = store.apply(store.empty_root_digest, sets={"key": {"value": 1}})
    assert store.get(committed.root_digest, "key") == {"value": 1}


def test_concurrent_identical_objects_are_idempotent(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_DURABILITY_PROFILE", "best_effort")
    values = {f"key-{index:04d}": {"value": index} for index in range(600)}

    def commit():
        return store_v2.ProjectionStoreV2(isolated_memory).apply(
            store_v2.EMPTY_ROOT_DIGEST,
            sets=values,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _index: commit(), range(4)))

    assert len({result.root_digest for result in results}) == 1
    store = store_v2.ProjectionStoreV2(isolated_memory)
    assert len(store.iter_items(results[0].root_digest, limit=1000)) == len(values)
    assert sum(result.new_objects for result in results) >= 1
    assert sum(result.reused_objects for result in results) >= 1


def test_full_profile_uses_existing_file_and_directory_barriers(
    isolated_memory,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []
    monkeypatch.setenv("VECTOR_LAKE_DURABILITY_PROFILE", "full")
    monkeypatch.setattr(
        durability,
        "sync_open_file",
        lambda handle: calls.append(("file", str(handle.name))),
    )
    monkeypatch.setattr(
        durability,
        "sync_directory",
        lambda path: calls.append(("directory", os.fspath(path))),
    )

    result = store_v2.ProjectionStoreV2(isolated_memory).apply(
        store_v2.EMPTY_ROOT_DIGEST,
        sets={"key": "value"},
    )

    assert result.new_objects == 1
    assert any(kind == "file" for kind, _path in calls)
    assert any(kind == "directory" for kind, _path in calls)


@pytest.mark.skipif(os.name != "nt", reason="Windows write-through acceptance")
def test_windows_full_profile_round_trip(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_DURABILITY_PROFILE", "full")
    store = store_v2.ProjectionStoreV2(isolated_memory)

    result = store.apply(store.empty_root_digest, sets={"key": {"value": 1}})

    assert result.new_objects == 1
    assert store.get(result.root_digest, "key") == {"value": 1}


def test_single_key_write_bytes_do_not_scale_linearly_from_10k_to_100k(
    isolated_memory,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_DURABILITY_PROFILE", "best_effort")

    def sample(base: Path, count: int) -> tuple[list[int], int]:
        store = store_v2.ProjectionStoreV2(base)
        padding = "p" * 160
        initial = store.apply(
            store.empty_root_digest,
            sets={
                f"key-{index:06d}": {"payload": padding, "revision": 0}
                for index in range(count)
            },
        )
        root = initial.root_digest
        observed: list[int] = []
        for revision in range(1, 25):
            target = f"key-{((revision * 7919) % count):06d}"
            changed = store.apply(
                root,
                sets={target: {"payload": padding, "revision": revision}},
            )
            assert changed.new_objects <= store_v2.MAX_DEPTH + 1
            observed.append(changed.new_bytes)
            root = changed.root_digest
        return observed, initial.new_bytes

    ten_k, ten_k_initial_bytes = sample(isolated_memory / "n10k", 10_000)
    hundred_k, hundred_k_initial_bytes = sample(
        isolated_memory / "n100k",
        100_000,
    )
    p95_ten_k = statistics.quantiles(ten_k, n=20, method="inclusive")[18]
    p95_hundred_k = statistics.quantiles(
        hundred_k,
        n=20,
        method="inclusive",
    )[18]

    assert hundred_k_initial_bytes > ten_k_initial_bytes * 5
    assert p95_hundred_k / p95_ten_k <= 1.10
    assert p95_hundred_k < math.ceil(hundred_k_initial_bytes * 0.01)
