import json
from vector_lake.db_store import get_connection

def search_timeline_events(entity_name: str = None, sentiment: str = None, action: str = None, limit: int = 10) -> str:
    """Query the timeline-event claims from SQLite for strategic events."""
    conn = get_connection()
    cursor = conn.cursor()
    
    query = "SELECT claim_text, data_json, updated_at FROM timeline_events WHERE 1=1"
    params = []
    
    if entity_name:
        query += " AND (entity_id LIKE ? OR claim_text LIKE ?)"
        params.extend([f"%{entity_name}%", f"%{entity_name}%"])
        
    query += f" ORDER BY event_date DESC, updated_at DESC LIMIT {int(limit)}"
    
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