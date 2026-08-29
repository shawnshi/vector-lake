"""Thread-safe shared caches derived from the durable index projection."""

from __future__ import annotations

import json
import os
import re
import threading
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

try:
    import orjson as _orjson
except ImportError:  # pragma: no cover - exercised by minimal installations
    _orjson = None


_ORJSON_OPT_IN_ENV = "VECTOR_LAKE_INDEX_USE_ORJSON"
_STDLIB_FULL_LOAD_ENV = "VECTOR_LAKE_INDEX_FULL_LOAD"
_GRAPH_CACHE_MIB_ENV = "VECTOR_LAKE_GRAPH_CACHE_MAX_MIB"
_DEFAULT_GRAPH_CACHE_MIB = 64
_MIN_GRAPH_CACHE_MIB = 8
_MAX_GRAPH_CACHE_MIB = 512
_GRAPH_EDGE_BYTES_ESTIMATE = 24
_GRAPH_NODE_BYTES_ESTIMATE = 160
_MAX_JSON_NESTING_DEPTH = 4_096
_STRING_ESCAPE_OR_CONTROL_RE = re.compile(rb"[\\\x00-\x1f]")

_CACHE_LOCK = threading.RLock()
_GRAPH_BUILD_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"key": None, "value": None}
_GRAPH_UNBUILT = object()
_GRAPH_UNAVAILABLE = object()
_GRAPH_CACHE: dict[str, Any] = {
    "snapshot": None,
    "edges": None,
    "value": _GRAPH_UNBUILT,
}


def _reject_snapshot_mutation(*_args, **_kwargs):
    raise TypeError("shared index snapshots are read-only")


class FrozenDict(dict):
    """JSON-compatible dict that blocks normal mutation APIs."""

    __slots__ = ()

    __setitem__ = _reject_snapshot_mutation
    __delitem__ = _reject_snapshot_mutation
    __ior__ = _reject_snapshot_mutation
    clear = _reject_snapshot_mutation
    pop = _reject_snapshot_mutation
    popitem = _reject_snapshot_mutation
    setdefault = _reject_snapshot_mutation
    update = _reject_snapshot_mutation


class FrozenList(list):
    """JSON-compatible list that blocks normal mutation APIs."""

    __slots__ = ()

    __setitem__ = _reject_snapshot_mutation
    __delitem__ = _reject_snapshot_mutation
    __iadd__ = _reject_snapshot_mutation
    __imul__ = _reject_snapshot_mutation
    append = _reject_snapshot_mutation
    clear = _reject_snapshot_mutation
    extend = _reject_snapshot_mutation
    insert = _reject_snapshot_mutation
    pop = _reject_snapshot_mutation
    remove = _reject_snapshot_mutation
    reverse = _reject_snapshot_mutation
    sort = _reject_snapshot_mutation


def _freeze_json_tree(value: Any) -> Any:
    """Freeze a freshly decoded tree while preserving dict/list compatibility."""
    if isinstance(value, dict):
        for key in value:
            dict.__setitem__(value, key, _freeze_json_tree(value[key]))
        return FrozenDict(value)
    if isinstance(value, list):
        for index, item in enumerate(value):
            list.__setitem__(value, index, _freeze_json_tree(item))
        return FrozenList(value)
    return value



class _IncrementalJsonDecoder:
    """Decode JSON incrementally from a read-only memory map.

    The standard ``json.load`` implementation materializes the complete UTF-8
    document as a second Python string. Large indexes therefore need the final
    object graph plus several times the file size during initial decode. This
    decoder retains one immutable byte buffer and copies one scalar token at a
    time while constructing the same deeply read-only dict/list-compatible
    tree. The source file is closed before parsing so Windows projection swaps
    are not blocked for the duration of the decode.
    """

    __slots__ = ("_data", "_key_cache", "_length")

    def __init__(self, data: bytes):
        self._data = data
        self._key_cache: dict[str, str] = {}
        self._length = len(data)

    def decode(self) -> FrozenDict:
        if not self._length:
            raise ValueError("index snapshot is empty")
        position = self._skip_ws(0)
        root, position, container_kind = self._parse_token(position)
        stack: list[list[Any]] = []
        if container_kind is not None:
            stack.append([container_kind, root, 0, None])

        while stack:
            frame = stack[-1]
            kind = frame[0]
            state = frame[2]
            position = self._skip_ws(position)
            marker = self._data[position] if position < self._length else None

            if kind == "array":
                if state in (0, 1):  # first-or-end / value-required
                    if state == 0 and marker == 93:  # ]
                        stack.pop()
                        position += 1
                        continue
                    value, position, child_kind = self._parse_token(position)
                    list.append(frame[1], value)
                    frame[2] = 2  # comma-or-end
                    if child_kind is not None:
                        self._push_container(
                            stack,
                            child_kind,
                            value,
                            position,
                        )
                    continue
                if marker == 93:  # ]
                    stack.pop()
                    position += 1
                    continue
                if marker != 44:  # ,
                    self._fail("expected ',' or ']'", position)
                frame[2] = 1  # value-required
                position += 1
                continue

            if state in (0, 1):  # first-key-or-end / key-required
                if state == 0 and marker == 125:  # }
                    stack.pop()
                    position += 1
                    continue
                if marker != 34:  # "
                    self._fail("object key must be a string", position)
                key, position = self._parse_string(position)
                frame[3] = self._key_cache.setdefault(key, key)
                frame[2] = 2  # colon-required
                continue
            if state == 2:
                if marker != 58:  # :
                    self._fail("expected ':' after object key", position)
                frame[2] = 3  # value-required
                position += 1
                continue
            if state == 3:
                value, position, child_kind = self._parse_token(position)
                dict.__setitem__(frame[1], frame[3], value)
                frame[3] = None
                frame[2] = 4  # comma-or-end
                if child_kind is not None:
                    self._push_container(
                        stack,
                        child_kind,
                        value,
                        position,
                    )
                continue
            if marker == 125:  # }
                stack.pop()
                position += 1
                continue
            if marker != 44:  # ,
                self._fail("expected ',' or '}'", position)
            frame[2] = 1  # key-required
            position += 1

        position = self._skip_ws(position)
        if position != self._length:
            self._fail("unexpected trailing data", position)
        if not isinstance(root, FrozenDict):
            raise ValueError("index snapshot root must be a JSON object")
        return root

    def _push_container(
        self,
        stack: list[list[Any]],
        kind: str,
        value: Any,
        position: int,
    ) -> None:
        if len(stack) >= _MAX_JSON_NESTING_DEPTH:
            self._fail(
                f"JSON nesting exceeds {_MAX_JSON_NESTING_DEPTH}",
                position,
            )
        stack.append([kind, value, 0, None])

    def _skip_ws(self, position: int) -> int:
        data = self._data
        length = self._length
        while position < length and data[position] in (9, 10, 13, 32):
            position += 1
        return position

    @staticmethod
    def _fail(message: str, position: int):
        raise ValueError(f"{message} at byte {position}")

    def _parse_token(self, position: int) -> tuple[Any, int, str | None]:
        if position >= self._length:
            self._fail("unexpected end of JSON", position)
        marker = self._data[position]
        if marker == 123:  # {
            return FrozenDict(), position + 1, "object"
        if marker == 91:  # [
            return FrozenList(), position + 1, "array"
        if marker == 34:  # "
            value, position = self._parse_string(position)
            return value, position, None
        value, position = self._parse_atom(position)
        return value, position, None

    def _parse_string(self, position: int) -> tuple[str, int]:
        data = self._data
        start = position
        quote = data.find(b'"', position + 1)
        while quote >= 0:
            backslash_count = 0
            cursor = quote - 1
            while cursor > start and data[cursor] == 92:  # backslash
                backslash_count += 1
                cursor -= 1
            if backslash_count % 2 == 0:
                end = quote + 1
                fragment = data[start + 1 : quote]
                try:
                    if _STRING_ESCAPE_OR_CONTROL_RE.search(fragment) is None:
                        return fragment.decode("utf-8"), end
                    return json.loads(data[start:end].decode("utf-8")), end
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        f"invalid JSON string at byte {start}"
                    ) from exc
            quote = data.find(b'"', quote + 1)
        self._fail("unterminated string", start)

    def _parse_atom(self, position: int) -> tuple[Any, int]:
        start = position
        data = self._data
        while position < self._length:
            marker = data[position]
            if marker in (9, 10, 13, 32, 44, 93, 125):
                break
            position += 1
        if position == start:
            self._fail("expected JSON value", position)
        token = data[start:position]
        if token == b"true":
            return True, position
        if token == b"false":
            return False, position
        if token == b"null":
            return None, position
        try:
            return json.loads(token.decode("utf-8")), position
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON value at byte {start}") from exc


def _decode_index_snapshot_streaming(path: Path) -> FrozenDict:
    payload = path.read_bytes()
    if not payload:
        raise ValueError("index snapshot is empty")
    return _IncrementalJsonDecoder(payload).decode()


def _decode_index_snapshot(path: Path) -> FrozenDict:
    use_orjson = (
        _orjson is not None
        and os.getenv(_ORJSON_OPT_IN_ENV, "").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    if use_orjson:
        value = _orjson.loads(path.read_bytes())
    elif os.getenv(_STDLIB_FULL_LOAD_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    else:
        return _decode_index_snapshot_streaming(path)
    if not isinstance(value, dict):
        raise ValueError("index snapshot root must be a JSON object")
    return _freeze_json_tree(value)


@dataclass(frozen=True, slots=True)
class CompactGraphAdjacency:
    """Immutable interface over compact CSR-style undirected adjacency arrays."""

    _node_keys: tuple[str, ...]
    _node_to_index: FrozenDict
    _offsets: array
    _neighbors: array
    _weights: array
    _total_weights: array
    edge_count: int

    @property
    def node_count(self) -> int:
        return len(self._node_keys)

    @property
    def directed_entry_count(self) -> int:
        return len(self._neighbors)

    def total_weight(self, node_key: str) -> float:
        node_index = self._node_to_index.get(node_key)
        if node_index is None:
            return 0.0
        return self._total_weights[node_index]

    def iter_weighted_neighbors(
        self,
        node_key: str,
    ) -> Iterator[tuple[str, float]]:
        node_index = self._node_to_index.get(node_key)
        if node_index is None:
            return
        start = self._offsets[node_index]
        end = self._offsets[node_index + 1]
        for position in range(start, end):
            yield (
                self._node_keys[self._neighbors[position]],
                self._weights[position],
            )


class _GraphCacheCapacityExceeded(RuntimeError):
    pass


def _graph_cache_budget_bytes() -> int:
    raw_value = os.getenv(_GRAPH_CACHE_MIB_ENV, str(_DEFAULT_GRAPH_CACHE_MIB))
    try:
        requested_mib = int(raw_value)
    except (TypeError, ValueError):
        requested_mib = _DEFAULT_GRAPH_CACHE_MIB
    bounded_mib = max(
        _MIN_GRAPH_CACHE_MIB,
        min(_MAX_GRAPH_CACHE_MIB, requested_mib),
    )
    return bounded_mib * 1024 * 1024


def _estimated_graph_cache_bytes(edge_count: int, node_count: int) -> int:
    return (
        edge_count * _GRAPH_EDGE_BYTES_ESTIMATE
        + node_count * _GRAPH_NODE_BYTES_ESTIMATE
    )


def _build_compact_graph_adjacency(
    weighted_edges: list[dict],
) -> CompactGraphAdjacency:
    edge_count = len(weighted_edges)
    budget_bytes = _graph_cache_budget_bytes()
    if _estimated_graph_cache_bytes(edge_count, 0) > budget_bytes:
        raise _GraphCacheCapacityExceeded

    node_to_index: dict[str, int] = {}
    node_keys: list[str] = []
    degrees = array("I")

    def index_for(node_key: str) -> int:
        node_index = node_to_index.get(node_key)
        if node_index is not None:
            return node_index
        prospective_node_count = len(node_keys) + 1
        if (
            _estimated_graph_cache_bytes(edge_count, prospective_node_count)
            > budget_bytes
        ):
            raise _GraphCacheCapacityExceeded
        node_index = len(node_keys)
        node_to_index[node_key] = node_index
        node_keys.append(node_key)
        degrees.append(0)
        return node_index

    for edge in weighted_edges:
        source_index = index_for(edge["source"])
        target_index = index_for(edge["target"])
        degrees[source_index] += 1
        degrees[target_index] += 1

    offsets = array("I", [0])
    for degree in degrees:
        offsets.append(offsets[-1] + degree)
    neighbors = array("I", [0]) * offsets[-1]
    weights = array("d", [0.0]) * offsets[-1]
    total_weights = array("d", [0.0]) * len(node_keys)
    cursors = array("I", offsets[:-1])

    for edge in weighted_edges:
        source_index = node_to_index[edge["source"]]
        target_index = node_to_index[edge["target"]]
        weight = float(edge.get("weight", 1.0))

        position = cursors[source_index]
        neighbors[position] = target_index
        weights[position] = weight
        total_weights[source_index] += weight
        cursors[source_index] += 1

        position = cursors[target_index]
        neighbors[position] = source_index
        weights[position] = weight
        total_weights[target_index] += weight
        cursors[target_index] += 1

    return CompactGraphAdjacency(
        _node_keys=tuple(node_keys),
        _node_to_index=FrozenDict(node_to_index),
        _offsets=offsets,
        _neighbors=neighbors,
        _weights=weights,
        _total_weights=total_weights,
        edge_count=len(weighted_edges),
    )


def _reset_graph_cache_locked() -> None:
    _GRAPH_CACHE.update(
        {
            "snapshot": None,
            "edges": None,
            "value": _GRAPH_UNBUILT,
        }
    )


def index_snapshot_identity(path: str | Path) -> tuple:
    resolved = Path(path).resolve()
    stat = resolved.stat()
    identity: tuple = (
        str(resolved),
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_size,
    )
    # The v2 index filename is deliberately static. Its cache identity must
    # therefore include the actual commit marker, otherwise a new generation
    # would keep returning the object decoded for the previous sidecar.
    from vector_lake.projection_format_v2 import (
        SIDECAR_FILENAME,
        is_v2_locator,
        read_committed_sidecar,
    )

    if is_v2_locator(resolved, "index"):
        marker = resolved.with_name(SIDECAR_FILENAME)
        try:
            marker_stat = marker.stat()
        except OSError as exc:
            from vector_lake.projection_format_v2 import ProjectionV2ContractError

            raise ProjectionV2ContractError("sidecar_unreadable") from exc
        # A static v2 locator and an unchanged sidecar are not sufficient for a
        # cache hit: canonical SQLite state may have advanced without a new
        # projection being published yet.  Revalidate the small commit marker
        # and schema-v9 runtime row on every read, while retaining the already
        # materialized immutable snapshot when that binding is still current.
        sidecar, committed_identity, runtime = read_committed_sidecar(
            resolved.parent
        )
        identity += (
            marker_stat.st_dev,
            marker_stat.st_ino,
            marker_stat.st_mtime_ns,
            marker_stat.st_ctime_ns,
            marker_stat.st_size,
            committed_identity,
            sidecar["projection_generation"],
            runtime.get("sidecar_sha256"),
        )
    return identity


def load_index_snapshot(path: str | Path) -> dict[str, Any]:
    """Return one read-only parsed object for an unchanged index file identity."""
    resolved = Path(path).resolve()
    key = index_snapshot_identity(resolved)
    from vector_lake.projection_format_v2 import (
        ProjectionV2ContractError,
        is_v2_locator,
        load_committed_index,
    )

    if not is_v2_locator(resolved, "index"):
        raise ProjectionV2ContractError(
            "legacy_index_requires_explicit_migration_reader"
        )
    with _CACHE_LOCK:
        if _CACHE.get("key") == key:
            return _CACHE["value"]
        value = _freeze_json_tree(load_committed_index(resolved.parent))
        _CACHE.update({"key": key, "value": value})
        _reset_graph_cache_locked()
        return value


def load_legacy_index_snapshot_for_migration(
    path: str | Path,
) -> dict[str, Any]:
    """Decode legacy payload bytes only for explicit migration/rollback code."""
    resolved = Path(path).resolve()
    key = ("legacy_migration", index_snapshot_identity(resolved))
    from vector_lake.projection_format_v2 import (
        ProjectionV2ContractError,
        is_v2_locator,
    )

    if is_v2_locator(resolved, "index"):
        raise ProjectionV2ContractError("v2_locator_passed_to_legacy_reader")
    with _CACHE_LOCK:
        if _CACHE.get("key") == key:
            return _CACHE["value"]
        value = _freeze_json_tree(_decode_index_snapshot(resolved))
        _CACHE.update({"key": key, "value": value})
        _reset_graph_cache_locked()
        return value


def get_cached_index_snapshot(path: str | Path) -> dict[str, Any] | None:
    """Return the cached object only when it still matches the current file."""
    try:
        key = index_snapshot_identity(path)
    except OSError:
        return None
    with _CACHE_LOCK:
        if _CACHE.get("key") == key:
            return _CACHE["value"]
    return None


def get_compact_graph_adjacency(
    snapshot: dict[str, Any],
) -> CompactGraphAdjacency | None:
    """Return one bounded compact adjacency object per shared snapshot."""
    weighted_edges = snapshot.get("weighted_edges") or []
    with _CACHE_LOCK:
        if (
            _GRAPH_CACHE.get("snapshot") is snapshot
            and _GRAPH_CACHE.get("edges") is weighted_edges
        ):
            cached = _GRAPH_CACHE.get("value")
            if cached is _GRAPH_UNAVAILABLE:
                return None
            if cached is not _GRAPH_UNBUILT:
                return cached

    with _GRAPH_BUILD_LOCK:
        with _CACHE_LOCK:
            if (
                _GRAPH_CACHE.get("snapshot") is snapshot
                and _GRAPH_CACHE.get("edges") is weighted_edges
            ):
                cached = _GRAPH_CACHE.get("value")
                if cached is _GRAPH_UNAVAILABLE:
                    return None
                if cached is not _GRAPH_UNBUILT:
                    return cached

        try:
            adjacency = _build_compact_graph_adjacency(weighted_edges)
            cached_value: Any = adjacency
        except _GraphCacheCapacityExceeded:
            adjacency = None
            cached_value = _GRAPH_UNAVAILABLE

        with _CACHE_LOCK:
            current_snapshot = _CACHE.get("value")
            if current_snapshot is None or current_snapshot is snapshot:
                _GRAPH_CACHE.update(
                    {
                        "snapshot": snapshot,
                        "edges": weighted_edges,
                        "value": cached_value,
                    }
                )
        return adjacency


def clear_index_snapshot_cache_for_tests() -> None:
    with _CACHE_LOCK:
        _CACHE.update({"key": None, "value": None})
        _reset_graph_cache_locked()
