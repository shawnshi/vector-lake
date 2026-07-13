import sys
import os
sys.path.insert(0, os.path.abspath(r'C:\Users\shich\.gemini\config\plugins\vector-lake'))
from vector_lake.db_store import get_connection, transaction

with transaction():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE jobs SET status = 'queued' WHERE status = 'dispatched'")
    print(f'Reset {cursor.rowcount} orphaned jobs to queued.')
