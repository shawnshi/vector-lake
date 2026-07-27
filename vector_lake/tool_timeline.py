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


def _locator_search_terms(locator) -> set[str]:
    if not isinstance(locator, dict):
        return set()
    terms: set[str] = set()
    for value in locator.values():
        if isinstance(value, (str, int, float)):
            terms.add(str(value))
        elif isinstance(value, list):
            terms.update(
                str(item)
                for item in value
                if isinstance(item, (str, int, float))
            )
    return terms


def _entity_search_term_map(entity_ids: set[str]) -> dict[str, set[str]]:
    """Build canonical entity search terms without relying on denormalized claim columns."""
    if not entity_ids:
        return {}
    conn = get_connection()
    placeholders = ",".join("?" for _ in entity_ids)
    rows = conn.execute(
        f"SELECT entity_id, canonical_name, data_json FROM entities "
        f"WHERE entity_id IN ({placeholders})",
        tuple(sorted(entity_ids)),
    ).fetchall()
    terms_by_id: dict[str, set[str]] = {
        entity_id: {entity_id}
        for entity_id in entity_ids
    }
    for row in rows:
        entity_id = str(row["entity_id"])
        terms = terms_by_id.setdefault(entity_id, {entity_id})
        if row["canonical_name"]:
            terms.add(str(row["canonical_name"]))
        try:
            data = json.loads(row["data_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            data = {}
        for field in ("canonical_name", "title", "page_key", "name"):
            if data.get(field):
                terms.add(str(data[field]))
        aliases = data.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        terms.update(str(alias) for alias in aliases if alias)
        terms.update(_locator_search_terms(data.get("locator")))
    return terms_by_id


def _canonical_row_matches_entity(
    row,
    entity_name: str,
    entity_terms: dict[str, set[str]],
) -> bool:
    """Match a canonical claim using its payload and referenced entity records."""
    needle = str(entity_name).casefold()
    data = json.loads(row["data_json"])
    subjects = data.get("subject_entity_ids") or []
    if isinstance(subjects, str):
        subjects = [subjects]
    terms = {
        str(row["claim_text"] or ""),
        str(data.get("title") or ""),
        str(data.get("entity_title") or ""),
        str(data.get("source_page") or ""),
    }
    terms.update(_locator_search_terms(data.get("locator")))
    for subject in subjects:
        subject_id = str(subject)
        terms.add(subject_id)
        terms.update(entity_terms.get(subject_id, ()))
    return any(needle in term.casefold() for term in terms if term)


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
    """Compare canonical timeline events with the SQL projection."""
    conn = get_connection()
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
    entity_titles = _entity_title_map(entity_ids)

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
    if sentiment:
        query += " AND COALESCE(json_extract(data_json, '$.sentiment'), 'neutral') = ?"
        params.append(sentiment)
    if action:
        query += (
            " AND COALESCE("
            "json_extract(data_json, '$.action'), "
            "json_extract(data_json, '$.event_tag'), "
            "json_extract(data_json, '$.claim_type'), "
            "'timeline-event') LIKE ?"
        )
        params.append(f"%{action}%")

    query += " ORDER BY updated_at DESC"
    
    try:
        cursor.execute(query, params)
        rows = cursor.fetchall()
    except Exception as e:
        return f"Error executing timeline query: {e}"
        
    if entity_name and rows:
        entity_ids: set[str] = set()
        for row in rows:
            data = json.loads(row["data_json"])
            subjects = data.get("subject_entity_ids") or []
            if isinstance(subjects, str):
                subjects = [subjects]
            entity_ids.update(str(item) for item in subjects)
        entity_terms = _entity_search_term_map(entity_ids)
        rows = [
            row
            for row in rows
            if _canonical_row_matches_entity(row, entity_name, entity_terms)
        ]

    rows = rows[:max(1, int(limit))]
    if not rows:
        return "No timeline events found matching the criteria."

    entity_ids: set[str] = set()
    for row in rows:
        data = json.loads(row["data_json"])
        subjects = data.get("subject_entity_ids") or []
        if isinstance(subjects, str):
            subjects = [subjects]
        entity_ids.update(str(item) for item in subjects)
    entity_titles = _entity_title_map(entity_ids)
    events = [
        _event_from_claim_row(row, entity_titles=entity_titles)
        for row in rows
    ]
    return "\n\n".join(
        f"[{event['event_date']}] <{event['entity_title'] or event['entity_id']}>\n"
        f"  -> {event['description']}\n"
        f"  Action: {event['action']} | Sentiment: {event['sentiment']} | "
        f"Source: {event['source_file']}"
        for event in events
    )
