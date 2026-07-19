import hashlib
import json
from datetime import datetime, timezone
from vector_lake.db_store import get_connection


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


def _entity_title_map(entity_ids: set[str]) -> dict[str, str]:
    if not entity_ids:
        return {}
    conn = get_connection()
    placeholders = ",".join("?" for _ in entity_ids)
    rows = conn.execute(
        f"SELECT entity_id, canonical_name FROM entities WHERE entity_id IN ({placeholders})",
        tuple(sorted(entity_ids)),
    ).fetchall()
    return {str(row["entity_id"]): str(row["canonical_name"] or row["entity_id"]) for row in rows}


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


def timeline_projection_parity() -> dict:
    """Compare the exact stable event IDs in canonical claims and the SQL projection."""
    conn = get_connection()
    claim_rows = conn.execute(
        "SELECT claim_id, claim_text, data_json, updated_at FROM claims "
        "WHERE json_extract(data_json, '$.claim_type') = 'timeline-event'"
    ).fetchall()
    expected_ids = {_event_from_claim_row(row)["id"] for row in claim_rows}
    actual_ids = {str(row["id"]) for row in conn.execute("SELECT id FROM timeline_events")}
    return {
        "canonical": len(expected_ids),
        "projection": len(actual_ids),
        "missing": len(expected_ids - actual_ids),
        "extra": len(actual_ids - expected_ids),
    }


def rebuild_timeline_events_from_claims(dry_run: bool = True, limit: int | None = None) -> str:
    """Rebuild the timeline_events projection from timeline-event claims."""
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

def search_timeline_events(entity_name: str = None, sentiment: str = None, action: str = None, limit: int = 10) -> str:
    """Query the timeline_events projection; fall back to timeline-event claims if the projection is empty."""
    conn = get_connection()
    cursor = conn.cursor()

    parity = timeline_projection_parity()
    if parity["projection"] and not parity["missing"] and not parity["extra"]:
        query = "SELECT event_date, action, sentiment, description, entity_id, entity_title, source_file FROM timeline_events WHERE 1=1"
        params = []
        if entity_name:
            query += " AND (entity_id LIKE ? OR entity_title LIKE ? OR description LIKE ?)"
            params.extend([f"%{entity_name}%", f"%{entity_name}%", f"%{entity_name}%"])
        if sentiment:
            query += " AND sentiment = ?"
            params.append(sentiment)
        if action:
            query += " AND action LIKE ?"
            params.append(f"%{action}%")
        query += " ORDER BY event_date DESC LIMIT ?"
        params.append(max(1, int(limit)))
        try:
            cursor.execute(query, params)
            rows = cursor.fetchall()
        except Exception as e:
            return f"Error executing timeline query: {e}"
        if not rows:
            return "No timeline events found matching the criteria."
        return "\n\n".join(
            f"[{r['event_date']}] <{r['entity_title'] or r['entity_id']}>\n"
            f"  -> {r['description']}\n"
            f"  Action: {r['action']} | Sentiment: {r['sentiment']} | Source: {r['source_file']}"
            for r in rows
        )

    query = "SELECT claim_id, claim_text, data_json, updated_at FROM claims WHERE json_extract(data_json, '$.claim_type') = 'timeline-event'"
    params = []
    
    if entity_name:
        query += " AND (entity_id LIKE ? OR claim_text LIKE ?)"
        params.extend([f"%{entity_name}%", f"%{entity_name}%"])
    if sentiment:
        query += " AND COALESCE(json_extract(data_json, '$.sentiment'), 'neutral') = ?"
        params.append(sentiment)
    if action:
        query += " AND COALESCE(json_extract(data_json, '$.action'), json_extract(data_json, '$.event_tag'), '') LIKE ?"
        params.append(f"%{action}%")

    query += " ORDER BY updated_at DESC LIMIT ?"
    params.append(max(1, int(limit)))
    
    try:
        cursor.execute(query, params)
        rows = cursor.fetchall()
    except Exception as e:
        return f"Error executing timeline query: {e}"
        
    if not rows:
        return "No timeline events found matching the criteria."
        
    results = []
    for r in rows:
        data = json.loads(r["data_json"])
        date = data.get("temporal_anchor") or "Unknown Date"
        entities = ", ".join(data.get("subject_entity_ids", []))
        source = ", ".join(data.get("source_ids", []))
        results.append(f"[{date}] <{entities}>\n  -> {r['claim_text']}\n  Source: {source}")
        
    return "\n\n".join(results)
