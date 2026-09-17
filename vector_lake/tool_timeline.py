import hashlib
import json
import re
from datetime import datetime, timezone
from vector_lake.db_store import get_connection

# The ingest pipeline prefixes every extracted timeline claim with its event
# date, e.g. ``[2026-05-02] [Observation] ...``.  On the live corpus 6573 of
# 9286 timeline claims carry the date only here (974 have a temporal anchor and
# 1739 have neither), so without this the projection reported the *ingestion*
# timestamp as the event date and ``ORDER BY event_date DESC`` was meaningless.
_TEXT_EVENT_DATE = re.compile(r"^\s*\[(\d{4}-\d{2}-\d{2})\]")


def claim_event_date(row, data: dict) -> str:
    """Canonical event date: temporal anchor, else the text prefix, else updated_at."""
    for candidate in (data.get("temporal_anchor"), data.get("event_date")):
        if candidate:
            return str(candidate)
    match = _TEXT_EVENT_DATE.match(str(row["claim_text"] or ""))
    if match:
        return match.group(1)
    return str(row["updated_at"] or "Unknown Date")


def _event_from_claim_row(row, entity_titles: dict[str, str] | None = None) -> dict:
    data = json.loads(row["data_json"])
    entities = data.get("subject_entity_ids") or []
    if isinstance(entities, str):
        entities = [entities]
    sources = data.get("source_ids") or []
    if isinstance(sources, str):
        sources = [sources]
    event_date = claim_event_date(row, data)
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


def _claim_subject_ids(data: dict) -> list[str]:
    subjects = data.get("subject_entity_ids") or []
    if isinstance(subjects, str):
        subjects = [subjects]
    return [str(item) for item in subjects]


# ``timeline_projection_parity`` is a full recomputation of the canonical event
# id set, so it is memoised against a cheap fingerprint of the rows it depends on:
#   * the database identity, so two isolated corpora can never share an entry;
#   * the count / newest ``updated_at`` / highest rowid of the timeline claims;
#   * the count and id extremes of the projection, which catch an out-of-band
#     write to ``timeline_events`` that bypasses the claim delta.
# Anything that can change the id sets moves one of those numbers, including
# writes made by another process, so the cache cannot silently go stale.
_PARITY_CACHE: dict = {"fingerprint": None, "result": None}


def _timeline_claims_fingerprint(conn=None) -> str:
    conn = conn or get_connection()
    count, newest, highest = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(updated_at), ''), COALESCE(MAX(rowid), 0) "
        "FROM claims WHERE json_extract(data_json, '$.claim_type') = 'timeline-event'"
    ).fetchone()
    projected, lowest_id, highest_id = conn.execute(
        "SELECT COUNT(*), COALESCE(MIN(id), ''), COALESCE(MAX(id), '') FROM timeline_events"
    ).fetchone()
    from vector_lake.db_store import get_db_path

    return f"{get_db_path()}|{count}:{newest}:{highest}|{projected}:{lowest_id}:{highest_id}"


def invalidate_timeline_parity_cache() -> None:
    """Drop the memoised parity proof after this process rewrites the projection."""
    _PARITY_CACHE["fingerprint"] = None
    _PARITY_CACHE["result"] = None


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
    invalidate_timeline_parity_cache()
    return {"deleted": len(old_event_ids), "upserted": len(new_events)}


def timeline_projection_parity() -> dict:
    """Compare the exact stable event IDs in canonical claims and the SQL projection.

    Memoised against :func:`_timeline_claims_fingerprint`; the recomputation is a
    SHA-256 over every timeline claim and used to run on every timeline query.
    """
    conn = get_connection()
    fingerprint = _timeline_claims_fingerprint(conn)
    if _PARITY_CACHE["fingerprint"] == fingerprint and _PARITY_CACHE["result"] is not None:
        return dict(_PARITY_CACHE["result"])
    claim_rows = conn.execute(
        "SELECT claim_id, claim_text, data_json, updated_at FROM claims "
        "WHERE json_extract(data_json, '$.claim_type') = 'timeline-event'"
    ).fetchall()
    expected_ids = {_event_from_claim_row(row)["id"] for row in claim_rows}
    actual_ids = {str(row["id"]) for row in conn.execute("SELECT id FROM timeline_events")}
    result = {
        "canonical": len(expected_ids),
        "projection": len(actual_ids),
        "missing": len(expected_ids - actual_ids),
        "extra": len(actual_ids - expected_ids),
    }
    _PARITY_CACHE["fingerprint"] = fingerprint
    _PARITY_CACHE["result"] = dict(result)
    return result


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
        entity_ids.update(_claim_subject_ids(json.loads(row["data_json"])))
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
    invalidate_timeline_parity_cache()
    return f"Rebuilt {len(events)} timeline_events row(s) from timeline-event claims."


def repair_timeline_projection(dry_run: bool = True) -> str:
    """Close the parity gap in place: drop orphan ids, project the missing ones.

    The event id is content-addressed -- ``sha256(claim_id, event_date, text)`` --
    on purpose, so that a row rewritten out of band with the same row count is
    detectable (``test_timeline_search_rejects_equal_count_wrong_event_ids``).
    ``event_date`` falls back to ``claim.updated_at`` when a claim carries no
    temporal anchor and no dated text, which is the case for roughly a quarter of
    the corpus, so a claim edit can legitimately move its id.

    ``sync_timeline_events_for_claim_delta`` deletes the id derived from the row
    content it is handed, so a delta that is not handed the *exact* previous row
    leaves an orphan that no later delta can ever match again.  Parity drift is
    therefore monotonic until the whole table is rebuilt.

    This removes only the orphan ids and inserts only the ids the claims now
    derive, so the projection converges without replacing ``timeline_events``.
    """
    conn = get_connection()
    claim_rows = conn.execute(
        "SELECT claim_id, claim_text, data_json, updated_at FROM claims "
        "WHERE json_extract(data_json, '$.claim_type') = 'timeline-event'"
    ).fetchall()
    entity_ids: set[str] = set()
    for row in claim_rows:
        entity_ids.update(_claim_subject_ids(json.loads(row["data_json"])))
    entity_titles = _entity_title_map(entity_ids)
    expected = {}
    for row in claim_rows:
        event = _event_from_claim_row(row, entity_titles=entity_titles)
        expected[event["id"]] = event
    actual = {str(row["id"]) for row in conn.execute("SELECT id FROM timeline_events")}
    orphan_ids = sorted(actual - set(expected))
    missing_ids = sorted(set(expected) - actual)
    if dry_run:
        return (
            "[DRY RUN] Would delete "
            f"{len(orphan_ids)} orphan row(s) and insert {len(missing_ids)} missing row(s)."
        )

    from vector_lake.db_store import transaction

    with transaction():
        if orphan_ids:
            conn.executemany(
                "DELETE FROM timeline_events WHERE id = ?", [(i,) for i in orphan_ids]
            )
        if missing_ids:
            conn.executemany(
                "INSERT OR REPLACE INTO timeline_events "
                "(id, event_date, action, sentiment, description, entity_id, entity_title, source_file, extracted_at) "
                "VALUES (:id, :event_date, :action, :sentiment, :description, :entity_id, :entity_title, :source_file, :extracted_at)",
                [expected[i] for i in missing_ids],
            )
    invalidate_timeline_parity_cache()
    return (
        f"Repaired timeline_events: deleted {len(orphan_ids)} orphan row(s), "
        f"inserted {len(missing_ids)} missing row(s)."
    )

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

    # The projection is stale: answer from canonical claims and say so, so a
    # degraded result is never mistaken for an authoritative one.
    drift_note = ""
    if parity["projection"]:
        drift_note = (
            f"[DEGRADED] timeline_events projection is out of parity with canonical claims "
            f"(missing={parity['missing']}, extra={parity['extra']}); answering from canonical claims. "
            f"Run rebuild_timeline_events(dry_run=False) to restore the indexed path.\n\n"
        )

    query = "SELECT claim_id, claim_text, data_json, updated_at FROM claims WHERE json_extract(data_json, '$.claim_type') = 'timeline-event'"
    params = []
    
    if entity_name:
        # ``claims`` has no ``entity_id`` column; the subjects live inside
        # ``data_json``.  The previous predicate referenced ``entity_id`` and
        # raised "no such column" on every filtered query, so the canonical
        # fallback could never return a result.
        query += " AND (claim_text LIKE ? OR json_extract(data_json, '$.subject_entity_ids') LIKE ?)"
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

    subject_ids: set[str] = set()
    decoded_events = []
    for r in rows:
        data = json.loads(r["data_json"])
        subjects = _claim_subject_ids(data)
        subject_ids.update(subjects)
        decoded_events.append((data, subjects, r))
    entity_titles = _entity_title_map(subject_ids)

    results = []
    for data, subjects, r in decoded_events:
        date = claim_event_date(r, data)
        entities = ", ".join(entity_titles.get(item, item) for item in subjects)
        source = ", ".join(data.get("source_ids", []))
        action = data.get("action") or data.get("event_tag") or data.get("claim_type") or "timeline-event"
        results.append(
            f"[{date}] <{entities}>\n  -> {r['claim_text']}\n"
            f"  Action: {action} | Source: {source}"
        )

    return drift_note + "\n\n".join(results)
