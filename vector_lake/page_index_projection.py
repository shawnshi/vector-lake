"""SQLite projection of ``index.json`` for the read paths.

``index.json`` stays the sovereign, portable artifact: it is what an operator can
inspect, diff and ship.  But it is one document, so every reader had to parse the
whole thing and rebuild derived structures from scratch:

* ``_search_scored_pages`` parsed the file once per process (18 MB / 0.2 s on the
  live 7 178-node corpus, and the parsed dict is a much larger object graph), and
  rebuilt a 30 140-edge undirected adjacency dict on *every* query;
* ``assemble_context`` re-parsed the whole file to render a 50-line summary;
* at 10^5 nodes the file is the dominant read cost and the in-memory dict is the
  dominant memory cost.

This module projects what the read paths consume into SQLite:

``page_index_nodes``
    One row per node: the filter columns as real columns plus ``node_json`` for
    full fidelity, because ``filter_expr`` is a caller-supplied expression over
    arbitrary node fields.  Read paths fetch only the keys they actually need, so
    the 7 178-node dict never has to exist in memory.

``page_index_edges``
    ``weighted_edges`` with an explicit ``sequence`` column.  ``page_graph_edges``
    happens to mirror the same data on the live corpus, but it is maintained by a
    different writer, so the projection derives from the sovereign file instead of
    depending on that coincidence.  The sequence preserves ``index.json``'s edge
    order, which the two-step personalised PageRank walk is sensitive to.
    The undirected adjacency is built once per process and reused.

``page_index_state``
    ``(index_mtime, index_size)`` of the ``index.json`` this projection was built
    from, so an out-of-band rewrite is detected with one ``stat`` instead of a
    parse, and the projection repairs itself.
"""

from __future__ import annotations

import json
import logging
import threading

from vector_lake.db_store import get_connection, init_db, transaction
from vector_lake.wiki_utils import get_index_path

log = logging.getLogger("vector-lake-page-index")

ADJACENCY_CACHE_LIMIT = 1

def index_file_stamp() -> tuple[float, int] | None:
    path = get_index_path()
    if not path.exists():
        return None
    stat = path.stat()
    return (stat.st_mtime, stat.st_size)


def projection_state() -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT index_mtime, index_size, node_count, edge_count, updated_at "
        "FROM page_index_state WHERE singleton = 1"
    ).fetchone()
    return dict(row) if row is not None else None


def projection_is_current() -> bool:
    stamp = index_file_stamp()
    if stamp is None:
        return False
    state = projection_state()
    if state is None:
        return False
    return (float(state["index_mtime"]), int(state["index_size"])) == stamp


def _node_columns(key: str, node: dict) -> tuple:
    """Full column tuple, including ``node_key`` (which is the dict key, not ``node['id']``)."""
    return (
        str(key),
        str(node.get("id") or ""),
        str(node.get("title") or ""),
        str(node.get("type") or ""),
        str(node.get("status") or ""),
        str(node.get("domain") or ""),
        str(node.get("topic_cluster") or ""),
        json.dumps(node, ensure_ascii=False),
    )


def _node_insert() -> str:
    return (
        "INSERT OR REPLACE INTO page_index_nodes "
        "(node_key, node_id, title, type, status, domain, topic_cluster, node_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )


def refresh_page_index_projection(index_data: dict, node_keys=None) -> dict:
    """Project ``index_data`` into SQLite and record the index.json stamp.

    ``node_keys`` restricts the upsert to those nodes, which is what the indexer's
    partial-update path needs.  Nodes that disappeared still have to be removed,
    so that branch compares the projection against the authoritative key set
    rather than trusting the partial list.  The edge projection is maintained
    separately by ``db_store.replace_page_graph_edges_for_node``.
    """
    conn = get_connection()
    nodes = index_data.get("nodes") or {}
    partial_keys = None if node_keys is None else [str(key) for key in node_keys]
    if partial_keys is not None:
        projected = conn.execute("SELECT COUNT(*) FROM page_index_nodes").fetchone()[0]
        if projected != len(nodes):
            partial_keys = None  # a page was added or removed: refresh everything

    if partial_keys is None:
        rows = [_node_columns(str(key), node) for key, node in nodes.items()]
        with transaction():
            conn.execute("DELETE FROM page_index_nodes")
            if rows:
                conn.executemany(_node_insert(), rows)
        written = len(rows)
    else:
        rows = []
        for key in partial_keys:
            node = nodes.get(key)
            if node is None:
                continue
            rows.append(_node_columns(str(key), node))
        with transaction():
            if rows:
                conn.executemany(_node_insert(), rows)
            conn.execute(
                "DELETE FROM page_index_nodes "
                "WHERE node_key NOT IN (SELECT value FROM json_each(?))",
                (json.dumps(sorted(str(key) for key in nodes)),),
            )
        written = len(rows)

    stamp = index_file_stamp()
    if "weighted_edges" in index_data:
        edge_rows = [
            (position, str(edge.get("source") or ""), str(edge.get("target") or ""),
             float(edge.get("weight", 1.0) or 0.0))
            for position, edge in enumerate(index_data.get("weighted_edges") or [])
        ]
        with transaction():
            conn.execute("DELETE FROM page_index_edges")
            if edge_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO page_index_edges "
                    "(sequence, source_key, target_key, weight) VALUES (?, ?, ?, ?)",
                    edge_rows,
                )
    if stamp is not None:
        with transaction():
            conn.execute(
                "INSERT INTO page_index_state "
                "(singleton, index_mtime, index_size, node_count, edge_count, updated_at) "
                "VALUES (1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "index_mtime = excluded.index_mtime, index_size = excluded.index_size, "
                "node_count = excluded.node_count, edge_count = excluded.edge_count, "
                "updated_at = excluded.updated_at",
                (
                    stamp[0], stamp[1],
                    conn.execute("SELECT COUNT(*) FROM page_index_nodes").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM page_index_edges").fetchone()[0],
                    _utc_now(),
                ),
            )
    _invalidate_adjacency()
    return {"nodes_written": written, "partial": partial_keys is not None}


def ensure_page_index_projection() -> bool:
    """Rebuild the node projection from ``index.json`` when it is behind.

    Returns False when there is no index at all, which callers report as the
    cold-start "lake is drying" state.
    """
    init_db()
    if projection_is_current():
        return True
    stamp = index_file_stamp()
    if stamp is None:
        return False
    path = get_index_path()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            index_data = json.load(handle)
    except Exception as exc:  # noqa: BLE001 - surfaced as a degraded read
        log.error("Failed to read index.json: %s", exc)
        raise
    refresh_page_index_projection(index_data)
    log.info(
        "Rebuilt the page index projection from %s (%s nodes).",
        path.name, len(index_data.get("nodes") or {}),
    )
    return True


def nodes_by_key(keys) -> dict[str, dict]:
    """Full node dicts for ``keys``, preserving the caller's key order."""
    ordered = [str(key) for key in keys]
    if not ordered:
        return {}
    conn = get_connection()
    found: dict[str, dict] = {}
    for start in range(0, len(ordered), 900):
        chunk = ordered[start : start + 900]
        placeholders = ",".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT node_key, node_json FROM page_index_nodes WHERE node_key IN ({placeholders})",
            chunk,
        ):
            found[str(row["node_key"])] = json.loads(row["node_json"])
    return {key: found[key] for key in ordered if key in found}


def node_summary_lines(limit: int = 50) -> list[str]:
    conn = get_connection()
    return [
        f"[{row['type'] or '?'}] {row['title'] or row['node_key']}"
        for row in conn.execute(
            "SELECT node_key, title, type FROM page_index_nodes ORDER BY node_key LIMIT ?",
            (int(limit),),
        )
    ]


# --- adjacency --------------------------------------------------------------

_adjacency_lock = threading.Lock()
_adjacency: dict = {"stamp": None, "data": None}


def _invalidate_adjacency() -> None:
    with _adjacency_lock:
        _adjacency["stamp"] = None
        _adjacency["data"] = None


def adjacency() -> dict[str, list[tuple[str, float]]]:
    """Undirected weighted adjacency, identical to the ``weighted_edges`` build.

    ``ORDER BY rowid`` reproduces ``index.json``'s edge order (verified against
    the live corpus), so the two-step personalised PageRank walk -- which is
    order-sensitive for zero-mass candidates -- stays bit-identical.
    """
    conn = get_connection()
    edge_count = conn.execute("SELECT COUNT(*) FROM page_index_edges").fetchone()[0]
    stamp = (str(get_index_path()), index_file_stamp(), int(edge_count or 0))
    with _adjacency_lock:
        if _adjacency["stamp"] == stamp and _adjacency["data"] is not None:
            return _adjacency["data"]
    table: dict[str, list[tuple[str, float]]] = {}
    for source, target, weight in conn.execute(
        "SELECT source_key, target_key, weight FROM page_index_edges ORDER BY sequence"
    ):
        source = str(source)
        target = str(target)
        weight = float(weight if weight is not None else 1.0)
        table.setdefault(source, []).append((target, weight))
        table.setdefault(target, []).append((source, weight))
    with _adjacency_lock:
        _adjacency["stamp"] = stamp
        _adjacency["data"] = table
    return table


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
