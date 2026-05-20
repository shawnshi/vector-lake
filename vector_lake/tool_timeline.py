import json
from vector_lake.db_store import get_connection

def search_timeline_events(entity_name: str = None, sentiment: str = None, action: str = None, limit: int = 10) -> str:
    """Query the timeline_events SQLite table for strategic events."""
    conn = get_connection()
    cursor = conn.cursor()
    
    query = "SELECT event_date, entity_title, action, sentiment, description, source_file FROM timeline_events WHERE 1=1"
    params = []
    
    if entity_name:
        query += " AND entity_title LIKE ?"
        params.append(f"%{entity_name}%")
        
    if sentiment:
        query += " AND sentiment = ?"
        params.append(sentiment)
        
    if action:
        query += " AND action = ?"
        params.append(action)
        
    query += f" ORDER BY event_date DESC LIMIT {int(limit)}"
    
    try:
        cursor.execute(query, params)
        rows = cursor.fetchall()
    except Exception as e:
        return f"Error executing timeline query: {e}"
        
    if not rows:
        return "No timeline events found matching the criteria."
        
    results = []
    for r in rows:
        results.append(f"[{r['event_date']}] <{r['entity_title']}> (Action: {r['action']} | Sentiment: {r['sentiment']})\n  -> {r['description']}\n  Source: {r['source_file']}")
        
    return "\n\n".join(results)