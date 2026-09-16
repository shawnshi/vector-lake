"""Read-only job queue report.

Run from the repository root:

    python check_jobs.py
"""
import json
import os
import sys

# Import the package from the repository it lives in; never from a hard-coded
# machine-specific path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vector_lake.db_store import get_connection, init_db  # noqa: E402


def main() -> int:
    init_db()
    conn = get_connection()

    print("Job statuses:")
    for row in conn.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status"):
        print(f"  {row['status']}: {row['count']}")

    filepaths = set()
    unparsable = 0
    for row in conn.execute("SELECT payload FROM jobs WHERE status = 'queued'"):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            unparsable += 1
            continue
        if isinstance(payload, dict) and payload.get("filepath"):
            filepaths.add(payload["filepath"])
    print(f"Distinct filepaths queued: {len(filepaths)}")
    if unparsable:
        print(f"Queued jobs with unreadable payloads: {unparsable}")

    terminal = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status IN ('failed') AND retries >= 3"
    ).fetchone()[0]
    print(f"Terminal failed jobs (retries >= 3): {terminal}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
