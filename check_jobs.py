import sys
import os
import json
sys.path.insert(0, os.path.abspath(r'C:\Users\shich\.gemini\config\plugins\vector-lake'))
from vector_lake.db_store import get_connection, transaction

with transaction():
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute('SELECT status, COUNT(*) FROM jobs GROUP BY status')
    print('All statuses:')
    for row in cursor.fetchall():
        print(f"  {row[0]}: {row[1]}")
        
    cursor.execute('SELECT payload FROM jobs WHERE status = "queued"')
    filepaths = set()
    for row in cursor.fetchall():
        try:
            p = json.loads(row[0])
            if "filepath" in p:
                filepaths.add(p["filepath"])
        except:
            pass
    print(f'Distinct filepaths queued: {len(filepaths)}')
    
    cursor.execute('SELECT COUNT(*) FROM jobs WHERE status = "finalized" OR status = "completed"')
    print(f'Completed/finalized jobs: {cursor.fetchone()[0]}')
