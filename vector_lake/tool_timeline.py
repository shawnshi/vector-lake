import hashlib
import json
from datetime import datetime, timezone
from vector_lake.db_store import get_connection


_STABLE_EVENT_PAYLOAD_FIELDS = (
    "event_date",
    "action",
    "sentiment",
    "description",
    "entity_id",
    "entity_title",
    "source_file",
)
_TIMELINE_LIMIT_MAX = 100
_TIMELINE_OUTPUT_BYTE_LIMIT = 64 * 1024
_TIMELINE_FILTER_CHAR_LIMIT = 256


def _stable_event_payload_signature(event) -> str:
    payload = {field: event[field] for field in _STABLE_EVENT_PAYLOAD_FIELDS}
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _event_from_claim_row(row, entity_titles: dict[str, str] | None = None) -> dict:
    data = json.loads(row["data_json"])
    entities = data.get("subject_entity_ids") or []
    if isinstance(entities, str):
        entities = [entities]
    sources = data.get("source_ids") or []
    if isinstance(sources, str):
        sources = [sources]
    event_date = (
        data.get("temporal_anchor")
        or data.get("event_date")
        or data.get("updated_at")
        or "Unknown Date"
    )
    description = row["claim_text"]
    entity_id = entities[0] if entities else ""
    stable_raw = "\0".join([str(row["claim_id"]), str(event_date), str(description)])
    return {
        "id": hashlib.sha256(stable_raw.encode("utf-8")).hexdigest()[:24],
        "event_date": str(event_date),
        "action": str(data.get("action") or data.get("event_tag") or data.get("claim_type") or "timeline-event"),
        "sentiment": str(data.get("sentiment") or "neutral"),
        "description": description,
        "entity_id": entity_id,
        "entity_title": ", ".join(
            str((entity_titles or {}).get(str(item)) or item)
            for item in entities
        ),
        "source_file": ", ".join(str(item) for item in sources),
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }


def _entity_title_map(
    entity_ids: set[str],
    *,
    connection=None,
) -> dict[str, str]:
    if not entity_ids:
        return {}
    conn = connection or get_connection()
    placeholders = ",".join("?" for _ in entity_ids)
    rows = conn.execute(
        f"SELECT entity_id, canonical_name FROM entities WHERE entity_id IN ({placeholders})",
        tuple(sorted(entity_ids)),
    ).fetchall()
    return {str(row["entity_id"]): str(row["canonical_name"] or row["entity_id"]) for row in rows}


def _bounded_timeline_text(value, max_chars: int) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _format_timeline_rows(rows) -> str:
    marker = "\n\n[Timeline output truncated to the 65536-byte budget.]"
    marker_bytes = len(marker.encode("utf-8"))
    block_budget = _TIMELINE_OUTPUT_BYTE_LIMIT - marker_bytes
    blocks: list[str] = []
    used_bytes = 0
    truncated = False
    for row in rows:
        block = (
            f"[{_bounded_timeline_text(row['event_date'], 128)}] "
            f"<{_bounded_timeline_text(row['entity_title'] or row['entity_id'], 512)}>\n"
            f"  -> {_bounded_timeline_text(row['description'], 4096)}\n"
            f"  Action: {_bounded_timeline_text(row['action'], 512)} | "
            f"Sentiment: {_bounded_timeline_text(row['sentiment'], 128)} | "
            f"Source: {_bounded_timeline_text(row['source_file'], 1024)}"
        )
        separator_bytes = 2 if blocks else 0
        encoded_bytes = len(block.encode("utf-8"))
        if used_bytes + separator_bytes + encoded_bytes > block_budget:
            truncated = True
            break
        blocks.append(block)
        used_bytes += separator_bytes + encoded_bytes
    if not blocks:
        return "No timeline events found matching the criteria."
    result = "\n\n".join(blocks)
    if truncated:
        result += marker
    return result


def sync_timeline_events_for_claim_delta(old_claim_rows: list, proposed_claims: list[dict]) -> dict:
    """Apply a claim-scoped Timeline projection delta inside the caller's transaction."""
    conn = get_connection()
    old_events = [
        _event_from_claim_row(row)
        for row in old_claim_rows
        if json.loads(row["data_json"]).get("claim_type") == "timeline-event"
    ]
    old_event_ids = [event["id"] for event in old_events]
    if old_event_ids:
        conn.executemany("DELETE FROM timeline_events WHERE id = ?", [(event_id,) for event_id in old_event_ids])

    proposed_rows = []
    entity_ids: set[str] = set()
    for claim in proposed_claims:
        if claim.get("claim_type") != "timeline-event":
            continue
        subjects = claim.get("subject_entity_ids") or []
        if isinstance(subjects, str):
            subjects = [subjects]
        entity_ids.update(str(item) for item in subjects)
        proposed_rows.append({
            "claim_id": claim["claim_id"],
            "claim_text": claim.get("claim_text", ""),
            "data_json": json.dumps(claim, ensure_ascii=False),
            "updated_at": claim.get("updated_at") or datetime.now(timezone.utc).isoformat(),
        })
    entity_titles = _entity_title_map(entity_ids)
    new_events = [_event_from_claim_row(row, entity_titles=entity_titles) for row in proposed_rows]
    if new_events:
        conn.executemany(
            "INSERT OR REPLACE INTO timeline_events "
            "(id, event_date, action, sentiment, description, entity_id, entity_title, source_file, extracted_at) "
            "VALUES (:id, :event_date, :action, :sentiment, :description, :entity_id, :entity_title, :source_file, :extracted_at)",
            new_events,
        )
    return {"deleted": len(old_event_ids), "upserted": len(new_events)}


def timeline_projection_parity(*, connection=None) -> dict:
    """Compare canonical timeline events with the SQL projection."""
    conn = connection or get_connection()
    claim_rows = conn.execute(
        "SELECT claim_id, claim_text, data_json, updated_at FROM claims "
        "WHERE json_extract(data_json, '$.claim_type') = 'timeline-event'"
    ).fetchall()

    entity_ids: set[str] = set()
    for row in claim_rows:
        data = json.loads(row["data_json"])
        subjects = data.get("subject_entity_ids") or []
        if isinstance(subjects, str):
            subjects = [subjects]
        entity_ids.update(str(item) for item in subjects)
    entity_titles = _entity_title_map(entity_ids, connection=conn)

    expected_events = [
        _event_from_claim_row(row, entity_titles=entity_titles)
        for row in claim_rows
    ]
    actual_events = conn.execute(
        "SELECT id, event_date, action, sentiment, description, entity_id, "
        "entity_title, source_file FROM timeline_events"
    ).fetchall()
    expected_by_id = {
        str(event["id"]): _stable_event_payload_signature(event)
        for event in expected_events
    }
    actual_by_id = {
        str(event["id"]): _stable_event_payload_signature(event)
        for event in actual_events
    }
    expected_ids = set(expected_by_id)
    actual_ids = set(actual_by_id)
    payload_drift = {
        event_id
        for event_id in expected_ids & actual_ids
        if expected_by_id[event_id] != actual_by_id[event_id]
    }
    return {
        "canonical": len(expected_ids),
        "projection": len(actual_ids),
        "missing": len(expected_ids - actual_ids) + len(payload_drift),
        "extra": len(actual_ids - expected_ids) + len(payload_drift),
    }


def rebuild_timeline_events_from_claims(dry_run: bool = True, limit: int | None = None) -> str:
    """Rebuild the timeline_events projection from timeline-event claims."""
    if not dry_run and limit is not None:
        raise ValueError("limit is only supported for timeline rebuild dry-runs")
    conn = get_connection()
    query = (
        "SELECT claim_id, claim_text, data_json, updated_at FROM claims "
        "WHERE json_extract(data_json, '$.claim_type') = 'timeline-event' "
        "ORDER BY updated_at DESC"
    )
    params: list = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(max(1, int(limit)))
    rows = conn.execute(query, params).fetchall()
    entity_ids = set()
    for row in rows:
        data = json.loads(row["data_json"])
        subjects = data.get("subject_entity_ids") or []
        if isinstance(subjects, str):
            subjects = [subjects]
        entity_ids.update(str(item) for item in subjects)
    entity_titles = _entity_title_map(entity_ids)
    events = [_event_from_claim_row(row, entity_titles=entity_titles) for row in rows]
    if dry_run:
        return f"[DRY RUN] Would rebuild {len(events)} timeline_events row(s) from timeline-event claims."

    from vector_lake.db_store import transaction

    with transaction():
        conn.execute("DELETE FROM timeline_events")
        conn.executemany(
            "INSERT OR REPLACE INTO timeline_events "
            "(id, event_date, action, sentiment, description, entity_id, entity_title, source_file, extracted_at) "
            "VALUES (:id, :event_date, :action, :sentiment, :description, :entity_id, :entity_title, :source_file, :extracted_at)",
            events,
        )
    return f"Rebuilt {len(events)} timeline_events row(s) from timeline-event claims."

def search_timeline_events(
    entity_name: str = "",
    sentiment: str = "",
    action: str = "",
    limit: int = 10,
) -> str:
    """Query the bounded Timeline projection without running full parity.

    Canonical mutations and Timeline projection updates share one transaction.
    Full payload parity remains a Doctor/repair concern instead of request-path
    work. Exact filters use composite indexes; substring fallback preserves the
    existing discovery behavior while result count and bytes remain bounded.
    """
    try:
        bounded_limit = min(_TIMELINE_LIMIT_MAX, max(1, int(limit)))
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc

    filters = {
        "entity_name": str(entity_name or "").strip(),
        "sentiment": str(sentiment or "").strip(),
        "action": str(action or "").strip(),
    }
    for name, value in filters.items():
        if len(value) > _TIMELINE_FILTER_CHAR_LIMIT:
            raise ValueError(
                f"{name} exceeds {_TIMELINE_FILTER_CHAR_LIMIT} characters"
            )

    conn = get_connection()

    def execute_query(*, exact_text_filters: bool):
        query = (
            "SELECT id, event_date, action, sentiment, description, entity_id, "
            "entity_title, source_file FROM timeline_events WHERE 1=1"
        )
        params: list = []
        if filters["entity_name"]:
            if exact_text_filters:
                query += (
                    " AND (entity_id = ? COLLATE NOCASE "
                    "OR entity_title = ? COLLATE NOCASE)"
                )
                params.extend(
                    [filters["entity_name"], filters["entity_name"]]
                )
            else:
                query += (
                    " AND (instr(lower(COALESCE(entity_id, '')), lower(?)) > 0 "
                    "OR instr(lower(COALESCE(entity_title, '')), lower(?)) > 0 "
                    "OR instr(lower(COALESCE(description, '')), lower(?)) > 0)"
                )
                params.extend([filters["entity_name"]] * 3)
        if filters["sentiment"]:
            query += " AND sentiment = ?"
            params.append(filters["sentiment"])
        if filters["action"]:
            if exact_text_filters:
                query += " AND action = ? COLLATE NOCASE"
            else:
                query += (
                    " AND instr(lower(COALESCE(action, '')), lower(?)) > 0"
                )
            params.append(filters["action"])
        query += " ORDER BY event_date DESC, id ASC LIMIT ?"
        params.append(bounded_limit)
        return conn.execute(query, params).fetchall()

    try:
        rows = execute_query(exact_text_filters=True)
        if not rows and (filters["entity_name"] or filters["action"]):
            rows = execute_query(exact_text_filters=False)
    except Exception as exc:
        return f"Error executing timeline query: {exc}"
    return _format_timeline_rows(rows)
