import atexit
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from vector_lake.wiki_utils import get_meta_dir, normalize_semantic_text, peek_meta_dir

_LOCAL = threading.local()
_INIT_LOCK = threading.Lock()
_INITIALIZED_DB_PATHS: set[str] = set()
_CONNECTIONS_LOCK = threading.RLock()
_CONNECTIONS: dict[int, sqlite3.Connection] = {}
_IDENTITY_VALIDATION_TOKENS: dict[int, tuple] = {}
_RUNTIME_GENERATION_SCHEMA_TOKENS: dict[int, tuple[int, int]] = {}
_INGEST_TASK_CLEANUP_SCHEMA_TOKENS: dict[int, tuple[int, int]] = {}
_IDENTITY_GENERATION_SURFACES = (
    "canonical_identities",
    "claim_versions",
    "claims",
    "evidence",
    "evidence_versions",
)
_RUNTIME_GENERATION_SURFACES = frozenset(
    {
        "canonical_identities",
        "change_sets",
        "claim_graph_edges",
        "claim_versions",
        "claims",
        "entities",
        "evidence",
        "evidence_versions",
        "governance_queue",
        "operational_memory",
        "page_graph_edges",
        "sources",
        "timeline_events",
        "mutation_outbox",
        "jobs",
    }
)
_SQLITE_WRITE_WAIT_DEFAULT_SECONDS = 30.0
_SQLITE_WRITE_WAIT_MIN_SECONDS = 0.05
_SQLITE_WRITE_WAIT_MAX_SECONDS = 300.0


def _normalized_schema_sql(statement: object) -> str:
    sql = re.sub(
        r"\bIF\s+NOT\s+EXISTS\s+",
        "",
        str(statement or ""),
        flags=re.IGNORECASE,
    )
    return " ".join(sql.split()).rstrip(";")


def _schema_contract_checksum(statements: tuple[str, ...]) -> str:
    normalized = "\n".join(_normalized_schema_sql(item) for item in statements)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


_RUNTIME_GENERATIONS_TABLE_SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS runtime_generations (
    surface TEXT PRIMARY KEY,
    generation INTEGER NOT NULL DEFAULT 0
)
"""


def _runtime_generation_trigger_name(surface: str, operation: str) -> str:
    return f"trg_{surface}_generation_v3_{operation.casefold()}"


def _runtime_generation_trigger_sql(surface: str, operation: str) -> str:
    operation_name = operation.upper()
    trigger_name = _runtime_generation_trigger_name(surface, operation)
    return f"""
    CREATE TRIGGER IF NOT EXISTS {trigger_name}
    AFTER {operation_name} ON {surface}
    BEGIN
        UPDATE runtime_generations
        SET generation = generation + 1
        WHERE surface = '{surface}';
        SELECT RAISE(ABORT, 'runtime generation registry is incomplete: {surface}')
        WHERE NOT EXISTS (
            SELECT 1 FROM runtime_generations WHERE surface = '{surface}'
        );
    END
    """


_RUNTIME_GENERATION_TRIGGER_SCHEMA_V3 = tuple(
    _runtime_generation_trigger_sql(surface, operation)
    for surface in sorted(_RUNTIME_GENERATION_SURFACES)
    for operation in ("insert", "update", "delete")
)
_RUNTIME_GENERATION_SCHEMA_V3 = (
    _RUNTIME_GENERATIONS_TABLE_SCHEMA_V3,
    *_RUNTIME_GENERATION_TRIGGER_SCHEMA_V3,
)
_RUNTIME_GENERATION_SCHEMA_OBJECTS_V3 = (
    ("table", "runtime_generations", _RUNTIME_GENERATIONS_TABLE_SCHEMA_V3),
    *tuple(
        (
            "trigger",
            _runtime_generation_trigger_name(surface, operation),
            _runtime_generation_trigger_sql(surface, operation),
        )
        for surface in sorted(_RUNTIME_GENERATION_SURFACES)
        for operation in ("insert", "update", "delete")
    ),
)


_INGEST_TASK_CLEANUP_TABLE_SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS ingest_task_cleanup (
    cleanup_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    task_packet_path TEXT NOT NULL,
    expected_task_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    available_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    lease_until TEXT,
    lease_owner TEXT,
    lease_token TEXT,
    lease_generation INTEGER NOT NULL DEFAULT 0,
    UNIQUE(job_id, task_packet_path)
)
"""
_INGEST_TASK_CLEANUP_READY_INDEX_SCHEMA_V4 = """
CREATE INDEX IF NOT EXISTS idx_ingest_task_cleanup_ready
ON ingest_task_cleanup(status, available_at, lease_until, cleanup_id)
"""
_INGEST_TASK_CLEANUP_SCHEMA_V4 = (
    _INGEST_TASK_CLEANUP_TABLE_SCHEMA_V4,
    _INGEST_TASK_CLEANUP_READY_INDEX_SCHEMA_V4,
)
_INGEST_TASK_CLEANUP_COLUMN_CONTRACT_V4 = (
    ("cleanup_id", "INTEGER", False, True),
    ("job_id", "TEXT", True, False),
    ("task_packet_path", "TEXT", True, False),
    ("expected_task_id", "TEXT", True, False),
    ("status", "TEXT", True, False),
    ("attempt_count", "INTEGER", True, False),
    ("last_error", "TEXT", False, False),
    ("available_at", "TEXT", True, False),
    ("created_at", "TEXT", True, False),
    ("updated_at", "TEXT", True, False),
    ("completed_at", "TEXT", False, False),
    ("lease_until", "TEXT", False, False),
    ("lease_owner", "TEXT", False, False),
    ("lease_token", "TEXT", False, False),
    ("lease_generation", "INTEGER", True, False),
)
_INGEST_TASK_CLEANUP_DEFAULTS_V4 = {
    "status": "'pending'",
    "attempt_count": "0",
    "lease_generation": "0",
}
_INGEST_TASK_CLEANUP_CORE_COLUMNS_V4 = frozenset(
    {"cleanup_id", "job_id", "task_packet_path"}
)
_INGEST_TASK_CLEANUP_ADD_COLUMNS_V4 = (
    ("expected_task_id", "TEXT NOT NULL DEFAULT ''"),
    ("status", "TEXT NOT NULL DEFAULT 'pending'"),
    ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
    ("last_error", "TEXT"),
    ("available_at", "TEXT NOT NULL DEFAULT ''"),
    ("created_at", "TEXT NOT NULL DEFAULT ''"),
    ("updated_at", "TEXT NOT NULL DEFAULT ''"),
    ("completed_at", "TEXT"),
    ("lease_until", "TEXT"),
    ("lease_owner", "TEXT"),
    ("lease_token", "TEXT"),
    ("lease_generation", "INTEGER NOT NULL DEFAULT 0"),
)
_INGEST_TASK_CLEANUP_IDENTITY_INDEX_SCHEMA_V4 = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_ingest_task_cleanup_identity_v4
ON ingest_task_cleanup(job_id, task_packet_path)
"""

_CANONICAL_IDENTITIES_SCHEMA_V2 = (
    """
    CREATE TABLE IF NOT EXISTS canonical_identities (
        record_kind TEXT NOT NULL,
        record_id TEXT NOT NULL,
        page_key TEXT NOT NULL,
        identity_origin TEXT NOT NULL,
        data_json TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        PRIMARY KEY(record_kind, record_id),
        CHECK(record_kind IN ('claim', 'evidence')),
        CHECK(length(trim(record_id)) > 0),
        CHECK(length(trim(page_key)) > 0),
        CHECK(length(trim(identity_origin)) > 0)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_canonical_identities_page
    ON canonical_identities(page_key, record_kind)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_canonical_identities_owner_conflict
    BEFORE INSERT ON canonical_identities
    WHEN EXISTS (
        SELECT 1 FROM canonical_identities AS existing
        WHERE existing.record_kind = NEW.record_kind
          AND existing.record_id = NEW.record_id
    )
    BEGIN
        SELECT RAISE(ABORT, 'canonical identity registry is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_canonical_identities_append_only_update
    BEFORE UPDATE ON canonical_identities
    BEGIN
        SELECT RAISE(ABORT, 'canonical identity registry is append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS trg_canonical_identities_append_only_delete
    BEFORE DELETE ON canonical_identities
    BEGIN
        SELECT RAISE(ABORT, 'canonical identity registry is append-only');
    END
    """,
)
_CANONICAL_IDENTITIES_SCHEMA_OBJECTS_V2 = (
    ("table", "canonical_identities", _CANONICAL_IDENTITIES_SCHEMA_V2[0]),
    ("index", "idx_canonical_identities_page", _CANONICAL_IDENTITIES_SCHEMA_V2[1]),
    (
        "trigger",
        "trg_canonical_identities_owner_conflict",
        _CANONICAL_IDENTITIES_SCHEMA_V2[2],
    ),
    (
        "trigger",
        "trg_canonical_identities_append_only_update",
        _CANONICAL_IDENTITIES_SCHEMA_V2[3],
    ),
    (
        "trigger",
        "trg_canonical_identities_append_only_delete",
        _CANONICAL_IDENTITIES_SCHEMA_V2[4],
    ),
)
_SCHEMA_VERSION = 4
_SCHEMA_MIGRATIONS = {
    1: (
        "baseline_schema_v1",
        hashlib.sha256(b"vector-lake:baseline-schema-v1").hexdigest(),
    ),
    2: (
        "canonical_identity_ownership_v2",
        _schema_contract_checksum(_CANONICAL_IDENTITIES_SCHEMA_V2),
    ),
    3: (
        "persistent_runtime_generations_v3",
        _schema_contract_checksum(_RUNTIME_GENERATION_SCHEMA_V3),
    ),
    4: (
        "ingest_task_cleanup_contract_v4",
        _schema_contract_checksum(_INGEST_TASK_CLEANUP_SCHEMA_V4),
    ),
}


def _canonical_identity_schema_issues(conn: sqlite3.Connection) -> list[str]:
    issues = []
    for object_type, name, expected_sql in _CANONICAL_IDENTITIES_SCHEMA_OBJECTS_V2:
        row = conn.execute(
            "SELECT type, sql FROM sqlite_master WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            issues.append(f"canonical_identity_schema_missing:{object_type}:{name}")
            continue
        if str(row["type"] or "") != object_type:
            issues.append(f"canonical_identity_schema_type_mismatch:{name}")
            continue
        if _normalized_schema_sql(row["sql"]) != _normalized_schema_sql(expected_sql):
            issues.append(f"canonical_identity_schema_sql_mismatch:{name}")
    return issues


def _assert_identity_schema_contract(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    if version < 2:
        return
    issues = _canonical_identity_schema_issues(conn)
    if issues:
        raise RuntimeError(
            "Schema v2 identity contract is invalid: " + ", ".join(issues)
        )


def _runtime_generation_registry_issues(
    conn: sqlite3.Connection,
) -> list[str]:
    rows = conn.execute("SELECT surface FROM runtime_generations").fetchall()
    observed = {str(row[0]) for row in rows}
    return [
        f"runtime_generation_registry_missing:{surface}"
        for surface in sorted(_RUNTIME_GENERATION_SURFACES - observed)
    ]


def _runtime_generation_schema_issues(conn: sqlite3.Connection) -> list[str]:
    issues = []
    table_available = True
    expected_objects = {
        name: (object_type, expected_sql)
        for object_type, name, expected_sql in _RUNTIME_GENERATION_SCHEMA_OBJECTS_V3
    }
    placeholders = ", ".join("?" for _ in expected_objects)
    observed_objects = {
        str(row["name"]): row
        for row in conn.execute(
            f"SELECT name, type, sql FROM sqlite_master WHERE name IN ({placeholders})",
            tuple(expected_objects),
        ).fetchall()
    }
    expected_trigger_names = {
        name
        for name, (object_type, _expected_sql) in expected_objects.items()
        if object_type == "trigger"
    }
    namespace_trigger_names = {
        str(row["name"])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND name GLOB 'trg_*_generation_v*_*'"
        ).fetchall()
    }
    for name in sorted(namespace_trigger_names - expected_trigger_names):
        issues.append(f"runtime_generation_schema_unexpected:trigger:{name}")
    for name, (object_type, expected_sql) in expected_objects.items():
        row = observed_objects.get(name)
        if row is None:
            issues.append(f"runtime_generation_schema_missing:{object_type}:{name}")
            if object_type == "table":
                table_available = False
            continue
        if str(row["type"] or "") != object_type:
            issues.append(f"runtime_generation_schema_type_mismatch:{name}")
            if object_type == "table":
                table_available = False
            continue
        if _normalized_schema_sql(row["sql"]) != _normalized_schema_sql(expected_sql):
            issues.append(f"runtime_generation_schema_sql_mismatch:{name}")
    if table_available:
        issues.extend(_runtime_generation_registry_issues(conn))
    return issues


def _assert_runtime_generation_schema_contract(
    conn: sqlite3.Connection,
) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    if version < 3:
        return
    schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0] or 0)
    token = (version, schema_version)
    if _RUNTIME_GENERATION_SCHEMA_TOKENS.get(id(conn)) == token:
        issues = _runtime_generation_registry_issues(conn)
    else:
        issues = _runtime_generation_schema_issues(conn)
    if issues:
        raise RuntimeError(
            "Schema v3 runtime generation contract is invalid: " + ", ".join(issues)
        )
    _RUNTIME_GENERATION_SCHEMA_TOKENS[id(conn)] = token
    if isinstance(conn, _GenerationTrackingConnection):
        conn.enable_persistent_runtime_generation_triggers()


def _normalized_schema_default(value: object) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).split()).strip()
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1].strip()
    return normalized.casefold()


def _ingest_task_cleanup_has_identity_index(conn: sqlite3.Connection) -> bool:
    for index in conn.execute("PRAGMA index_list('ingest_task_cleanup')").fetchall():
        if int(index["unique"] or 0) != 1 or int(index["partial"] or 0) != 0:
            continue
        key_columns = conn.execute(
            "SELECT name, desc, coll FROM pragma_index_xinfo(?) "
            "WHERE key = 1 ORDER BY seqno",
            (str(index["name"]),),
        ).fetchall()
        columns = tuple(str(row["name"]) for row in key_columns)
        if columns != ("job_id", "task_packet_path"):
            continue
        if any(
            int(row["desc"] or 0) != 0
            or str(row["coll"] or "").casefold() != "binary"
            for row in key_columns
        ):
            continue
        return True
    return False


def _ingest_task_cleanup_schema_issues(conn: sqlite3.Connection) -> list[str]:
    issues = []
    table = conn.execute(
        "SELECT type FROM sqlite_master WHERE name = 'ingest_task_cleanup'"
    ).fetchone()
    if table is None:
        return ["ingest_task_cleanup_schema_missing:table:ingest_task_cleanup"]
    if str(table["type"] or "") != "table":
        return ["ingest_task_cleanup_schema_type_mismatch:ingest_task_cleanup"]

    observed = {
        str(row["name"]): row
        for row in conn.execute("PRAGMA table_info('ingest_task_cleanup')").fetchall()
    }
    expected_columns = {
        item[0] for item in _INGEST_TASK_CLEANUP_COLUMN_CONTRACT_V4
    }
    for name in sorted(set(observed) - expected_columns):
        issues.append(f"ingest_task_cleanup_schema_unexpected:column:{name}")
    for (
        name,
        column_type,
        not_null,
        primary_key,
    ) in _INGEST_TASK_CLEANUP_COLUMN_CONTRACT_V4:
        row = observed.get(name)
        if row is None:
            issues.append(f"ingest_task_cleanup_schema_missing:column:{name}")
            continue
        if str(row["type"] or "").strip().casefold() != column_type.casefold():
            issues.append(f"ingest_task_cleanup_schema_type_mismatch:column:{name}")
        if bool(row["notnull"]) != bool(not_null):
            issues.append(
                f"ingest_task_cleanup_schema_nullability_mismatch:column:{name}"
            )
        if bool(row["pk"]) != bool(primary_key):
            issues.append(
                f"ingest_task_cleanup_schema_primary_key_mismatch:column:{name}"
            )
        expected_default = _INGEST_TASK_CLEANUP_DEFAULTS_V4.get(name)
        if expected_default is not None and _normalized_schema_default(
            row["dflt_value"]
        ) != _normalized_schema_default(expected_default):
            issues.append(f"ingest_task_cleanup_schema_default_mismatch:column:{name}")

    if not _ingest_task_cleanup_has_identity_index(conn):
        issues.append(
            "ingest_task_cleanup_schema_missing:unique:job_id_task_packet_path"
        )

    ready_index = conn.execute(
        "SELECT type, sql FROM sqlite_master "
        "WHERE name = 'idx_ingest_task_cleanup_ready'"
    ).fetchone()
    if ready_index is None:
        issues.append(
            "ingest_task_cleanup_schema_missing:index:idx_ingest_task_cleanup_ready"
        )
    elif str(ready_index["type"] or "") != "index":
        issues.append(
            "ingest_task_cleanup_schema_type_mismatch:idx_ingest_task_cleanup_ready"
        )
    elif _normalized_schema_sql(ready_index["sql"]) != _normalized_schema_sql(
        _INGEST_TASK_CLEANUP_READY_INDEX_SCHEMA_V4
    ):
        issues.append(
            "ingest_task_cleanup_schema_sql_mismatch:idx_ingest_task_cleanup_ready"
        )
    return issues


def _assert_ingest_task_cleanup_schema_contract(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    if version < 4:
        return
    schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0] or 0)
    token = (version, schema_version)
    if _INGEST_TASK_CLEANUP_SCHEMA_TOKENS.get(id(conn)) == token:
        return
    issues = _ingest_task_cleanup_schema_issues(conn)
    if issues:
        raise RuntimeError(
            "Schema v4 ingest task cleanup contract is invalid: " + ", ".join(issues)
        )
    _INGEST_TASK_CLEANUP_SCHEMA_TOKENS[id(conn)] = token


def _identity_validation_token(conn: sqlite3.Connection) -> tuple:
    """Detect schema and identity-relevant writes without unrelated invalidation."""
    schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0] or 0)
    placeholders = ", ".join("?" for _ in _IDENTITY_GENERATION_SURFACES)
    rows = conn.execute(
        "SELECT surface, generation FROM runtime_generations "
        f"WHERE surface IN ({placeholders}) ORDER BY surface",
        _IDENTITY_GENERATION_SURFACES,
    ).fetchall()
    generations = tuple((str(row[0]), int(row[1])) for row in rows)
    if len(generations) != len(_IDENTITY_GENERATION_SURFACES):
        raise RuntimeError("Identity runtime generation registry is incomplete")
    return schema_version, generations


def _validate_cached_identity_state(conn: sqlite3.Connection) -> None:
    """Revalidate ownership after relevant changes and cache only a stable scan."""
    _assert_identity_schema_contract(conn)
    _assert_runtime_generation_schema_contract(conn)
    _assert_ingest_task_cleanup_schema_contract(conn)
    version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    if version < 2:
        return
    for _attempt in range(2):
        before = _identity_validation_token(conn)
        if _IDENTITY_VALIDATION_TOKENS.get(id(conn)) == before:
            return
        _validate_canonical_identity_registry(conn)
        _validate_canonical_identity_coverage(conn)
        after = _identity_validation_token(conn)
        if before == after:
            _IDENTITY_VALIDATION_TOKENS[id(conn)] = after
            return
    raise RuntimeError(
        "Canonical identity state changed during validation; retry after writers finish"
    )


def _schema_inspection_result(database_path: str | Path) -> dict:
    return {
        "database_path": str(database_path),
        "read_only": True,
        "supported_version": _SCHEMA_VERSION,
        "user_version": None,
        "ledger_exists": False,
        "ledger": [],
        "ready": False,
        "status": "missing",
        "issues": [],
    }


def inspect_schema_migration_connection(
    conn: sqlite3.Connection,
    database_path: str | Path = "<connection>",
) -> dict:
    """Validate schema and migration contracts on a caller-owned connection.

    The validator performs no database writes and never opens or closes another
    connection. This permits exact validation of immutable standalone backups
    without consulting WAL sidecars or a different snapshot.
    """
    result = _schema_inspection_result(database_path)
    previous_row_factory = conn.row_factory
    try:
        conn.row_factory = sqlite3.Row
        result["user_version"] = int(conn.execute("PRAGMA user_version").fetchone()[0])
        ledger_exists = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
        )
        result["ledger_exists"] = ledger_exists
        if ledger_exists:
            rows = conn.execute(
                "SELECT version, name, checksum, applied_at "
                "FROM schema_migrations ORDER BY version"
            ).fetchall()
            result["ledger"] = [
                {
                    "version": int(row["version"]),
                    "name": str(row["name"]),
                    "checksum": str(row["checksum"]),
                    "applied_at": str(row["applied_at"]),
                }
                for row in rows
            ]
        if int(result["user_version"] or 0) >= 2:
            identity_schema_issues = _canonical_identity_schema_issues(conn)
            result["issues"].extend(identity_schema_issues)
            if not identity_schema_issues:
                try:
                    _validate_canonical_identity_registry(conn)
                    _validate_canonical_identity_coverage(conn)
                except RuntimeError as exc:
                    result["issues"].append(f"canonical_identity_integrity:{exc}")
        if int(result["user_version"] or 0) >= 3:
            result["issues"].extend(_runtime_generation_schema_issues(conn))
        if int(result["user_version"] or 0) >= 4:
            result["issues"].extend(_ingest_task_cleanup_schema_issues(conn))
    except (OSError, sqlite3.Error) as exc:
        result["status"] = "invalid"
        result["issues"].append(f"schema_inspection_failed:{exc}")
        return result
    finally:
        conn.row_factory = previous_row_factory

    current_version = int(result["user_version"] or 0)
    if current_version > _SCHEMA_VERSION:
        result["issues"].append("database_schema_newer_than_runtime")
    if not result["ledger_exists"]:
        result["issues"].append("schema_migrations_missing")
    actual = {
        int(item["version"]): (str(item["name"]), str(item["checksum"]))
        for item in result["ledger"]
    }
    expected_versions = set(range(1, current_version + 1))
    if set(actual) != expected_versions:
        result["issues"].append("schema_ledger_version_mismatch")
    for version in sorted(expected_versions):
        if actual.get(version) != _SCHEMA_MIGRATIONS.get(version):
            result["issues"].append(f"schema_migration_mismatch:{version}")
    result["issues"] = list(dict.fromkeys(result["issues"]))
    result["ready"] = (
        current_version == _SCHEMA_VERSION
        and result["ledger_exists"]
        and not result["issues"]
    )
    result["status"] = (
        "ready"
        if result["ready"]
        else ("uninitialized" if current_version == 0 else "invalid")
    )
    return result


def inspect_schema_migration_state(
    db_path: str | Path | None = None,
) -> dict:
    """Inspect the schema ledger without creating or migrating a database."""
    path = Path(db_path) if db_path is not None else peek_db_path()
    result = _schema_inspection_result(path)
    if not path.exists():
        result["issues"].append("database_missing")
        return result

    conn = None
    try:
        conn = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
        conn.execute("PRAGMA query_only=ON")
        return inspect_schema_migration_connection(conn, path)
    except (OSError, sqlite3.Error) as exc:
        result["status"] = "invalid"
        result["issues"].append(f"schema_inspection_failed:{exc}")
        return result
    finally:
        if conn is not None:
            conn.close()


_SQL_IDENTIFIER = r'(?:`[^`]+`|"[^"]+"|\[[^\]]+\]|[A-Za-z_]\w*)'
_WRITE_SURFACE_PATTERN = re.compile(
    rf"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|"
    rf"UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)\s+"
    rf"(?:{_SQL_IDENTIFIER}\s*\.\s*)?(?P<table>{_SQL_IDENTIFIER})",
    re.IGNORECASE | re.DOTALL,
)


def _normalized_sql_identifier(value: str) -> str:
    identifier = str(value or "").strip()
    if len(identifier) >= 2 and (
        (identifier[0], identifier[-1]) in {('"', '"'), ("`", "`"), ("[", "]")}
    ):
        identifier = identifier[1:-1]
    return identifier.casefold()


def _runtime_surfaces_written(sql: str) -> set[str]:
    return {
        surface
        for match in _WRITE_SURFACE_PATTERN.finditer(str(sql or ""))
        if (surface := _normalized_sql_identifier(match.group("table")))
        in _RUNTIME_GENERATION_SURFACES
    }


class _GenerationTrackingCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=()):
        self.connection._mark_runtime_surfaces(sql)
        return super().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        self.connection._mark_runtime_surfaces(sql)
        return super().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script):
        self.connection.executescript(sql_script)
        return self


class _GenerationTrackingConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._generation_dirty_surfaces: set[str] = set()
        self._persistent_runtime_generation_triggers = False

    def enable_persistent_runtime_generation_triggers(self) -> None:
        """Use durable schema-v3 triggers while retaining dirty-read fencing."""
        self._persistent_runtime_generation_triggers = True

    def _mark_runtime_surfaces(self, sql: str) -> None:
        self._generation_dirty_surfaces.update(_runtime_surfaces_written(sql))

    def generation_dirty_snapshot(self) -> set[str]:
        return set(self._generation_dirty_surfaces)

    def restore_generation_dirty_snapshot(self, snapshot: set[str]) -> None:
        self._generation_dirty_surfaces = set(snapshot)

    def execute(self, sql, parameters=()):
        self._mark_runtime_surfaces(sql)
        return super().execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        self._mark_runtime_surfaces(sql)
        return super().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script):
        """Track script writes even when SQLite auto-commits the script."""
        surfaces = _runtime_surfaces_written(sql_script)
        self._generation_dirty_surfaces.update(surfaces)
        try:
            result = super().executescript(sql_script)
        except BaseException:
            if not self.in_transaction:
                if self._persistent_runtime_generation_triggers:
                    self._generation_dirty_surfaces.clear()
                elif self._generation_dirty_surfaces:
                    self._flush_runtime_generations()
                    sqlite3.Connection.commit(self)
                    self._generation_dirty_surfaces.clear()
            raise
        if not self.in_transaction:
            if self._persistent_runtime_generation_triggers:
                self._generation_dirty_surfaces.clear()
            elif self._generation_dirty_surfaces:
                self._flush_runtime_generations()
                sqlite3.Connection.commit(self)
                self._generation_dirty_surfaces.clear()
        return result

    def cursor(self, factory=None):
        return super().cursor(factory or _GenerationTrackingCursor)

    def _flush_runtime_generations(self) -> None:
        if not self._generation_dirty_surfaces:
            return
        surfaces = tuple(sorted(self._generation_dirty_surfaces))
        placeholders = ", ".join("?" for _ in surfaces)
        cursor = sqlite3.Connection.execute(
            self,
            f"UPDATE runtime_generations SET generation = generation + 1 "
            f"WHERE surface IN ({placeholders})",
            surfaces,
        )
        if cursor.rowcount != len(surfaces):
            raise RuntimeError(
                "runtime generation registry is incomplete for committed surfaces"
            )

    def commit(self):
        if self._persistent_runtime_generation_triggers:
            result = super().commit()
            self._generation_dirty_surfaces.clear()
            return result
        self._flush_runtime_generations()
        result = super().commit()
        self._generation_dirty_surfaces.clear()
        return result

    def rollback(self):
        try:
            return super().rollback()
        finally:
            self._generation_dirty_surfaces.clear()


def _close_tracked_connection(conn: sqlite3.Connection) -> None:
    """Close one registered handle exactly once across thread/global cleanup."""
    with _CONNECTIONS_LOCK:
        tracked = _CONNECTIONS.pop(id(conn), None)
        _IDENTITY_VALIDATION_TOKENS.pop(id(conn), None)
        _RUNTIME_GENERATION_SCHEMA_TOKENS.pop(id(conn), None)
        _INGEST_TASK_CLEANUP_SCHEMA_TOKENS.pop(id(conn), None)
    if tracked is None:
        return
    try:
        tracked.close()
    except sqlite3.Error:
        pass


class _ThreadConnectionOwner:
    """Release a thread-owned connection when its threading.local state dies."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def close(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is not None:
            _close_tracked_connection(conn)

    def detach(self) -> None:
        """Drop the local reference after another owner closed the handle."""
        self._conn = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            # Interpreter shutdown may have already torn down module globals.
            pass


def _load_sqlite_vec_extension(conn: sqlite3.Connection) -> None:
    """Import sqlite-vec lazily, while loading it into every new connection."""
    import sqlite_vec

    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)


def _job_idempotency_key(task_type: str, payload: dict | None) -> str | None:
    if task_type != "ingest" or not isinstance(payload, dict):
        return None
    filepath = payload.get("filepath")
    file_hash = payload.get("hash")
    canonical_name = payload.get("canonical_name")
    if not filepath or not file_hash:
        return None
    raw = "\0".join(
        ["ingest", str(filepath), str(file_hash), str(canonical_name or "")]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_db_path() -> Path:
    import os

    override = os.environ.get("VECTOR_LAKE_DB_PATH")
    if override:
        return Path(override)
    return get_meta_dir() / "vector_lake.db"


def peek_db_path() -> Path:
    """Resolve the canonical database path without creating meta state."""
    override = os.environ.get("VECTOR_LAKE_DB_PATH")
    if override:
        return Path(override)
    return peek_meta_dir() / "vector_lake.db"


def get_connection() -> sqlite3.Connection:
    db_path = get_db_path().resolve()
    db_key = str(db_path)
    conn = getattr(_LOCAL, "conn", None)
    with _CONNECTIONS_LOCK:
        tracked = conn is not None and id(conn) in _CONNECTIONS
    if conn is not None and (getattr(_LOCAL, "db_key", None) != db_key or not tracked):
        if tracked:
            close_connection()
        else:
            owner = getattr(_LOCAL, "connection_owner", None)
            if owner is not None:
                owner.detach()
            _LOCAL.conn = None
            _LOCAL.db_key = None
            _LOCAL.connection_owner = None
        conn = None
    if conn is None:
        conn = sqlite3.connect(
            str(db_path),
            timeout=30.0,
            check_same_thread=False,
            factory=_GenerationTrackingConnection,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA recursive_triggers=ON")
        try:
            _load_sqlite_vec_extension(conn)
        except BaseException:
            conn.close()
            raise
        with _CONNECTIONS_LOCK:
            _CONNECTIONS[id(conn)] = conn
        _LOCAL.conn = conn
        _LOCAL.db_key = db_key
        _LOCAL.connection_owner = _ThreadConnectionOwner(conn)
    return conn


def close_connection():
    conn = getattr(_LOCAL, "conn", None)
    if conn is not None:
        owner = getattr(_LOCAL, "connection_owner", None)
        if owner is not None:
            owner.close()
        else:
            _close_tracked_connection(conn)
        _LOCAL.conn = None
        _LOCAL.db_key = None
        _LOCAL.connection_owner = None
    _LOCAL.in_transaction = False
    _LOCAL.close_when_transaction_ends = False
    _LOCAL.transaction_depth = 0


def cleanup_connection_after_tool_call(*, failed: bool = False) -> None:
    """Retain a clean worker connection; discard failed or open transactions."""
    conn = getattr(_LOCAL, "conn", None)
    if conn is None:
        return
    if failed or getattr(_LOCAL, "in_transaction", False) or bool(conn.in_transaction):
        close_connection()


def close_all_connections() -> None:
    """Close tracked SQLite handles, including handles owned by worker threads."""
    with _CONNECTIONS_LOCK:
        connections = list(_CONNECTIONS.values())
        _CONNECTIONS.clear()
        _IDENTITY_VALIDATION_TOKENS.clear()
        _RUNTIME_GENERATION_SCHEMA_TOKENS.clear()
        _INGEST_TASK_CLEANUP_SCHEMA_TOKENS.clear()
    for conn in connections:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    local_conn = getattr(_LOCAL, "conn", None)
    if local_conn is not None:
        owner = getattr(_LOCAL, "connection_owner", None)
        if owner is not None:
            owner.detach()
        _LOCAL.conn = None
        _LOCAL.db_key = None
        _LOCAL.connection_owner = None
    _LOCAL.in_transaction = False
    _LOCAL.connection_scope_depth = 0
    _LOCAL.close_when_transaction_ends = False
    _LOCAL.transaction_depth = 0


atexit.register(close_all_connections)


@contextmanager
def connection_scope():
    """Reuse one thread-local handle within a call, then close it at the boundary."""
    depth = getattr(_LOCAL, "connection_scope_depth", 0)
    _LOCAL.connection_scope_depth = depth + 1
    try:
        yield get_connection()
    finally:
        remaining = max(0, getattr(_LOCAL, "connection_scope_depth", 1) - 1)
        _LOCAL.connection_scope_depth = remaining
        if remaining == 0:
            if getattr(_LOCAL, "in_transaction", False):
                _LOCAL.close_when_transaction_ends = True
            else:
                close_connection()


def _configured_transaction_max_wait_seconds() -> float:
    """Return a finite host-configured write-lock deadline."""
    raw = os.environ.get(
        "VECTOR_LAKE_SQLITE_WRITE_MAX_WAIT_SECONDS",
        str(_SQLITE_WRITE_WAIT_DEFAULT_SECONDS),
    )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = _SQLITE_WRITE_WAIT_DEFAULT_SECONDS
    if not math.isfinite(value):
        value = _SQLITE_WRITE_WAIT_DEFAULT_SECONDS
    return min(
        _SQLITE_WRITE_WAIT_MAX_SECONDS,
        max(_SQLITE_WRITE_WAIT_MIN_SECONDS, value),
    )


@contextmanager
def transaction(max_wait_seconds: float | None = None):
    """Open a write transaction with a bounded lock-acquisition deadline."""
    explicit_wait = None
    if max_wait_seconds is not None:
        explicit_wait = float(max_wait_seconds)
        if not math.isfinite(explicit_wait):
            raise ValueError("max_wait_seconds must be finite")
        explicit_wait = min(
            _SQLITE_WRITE_WAIT_MAX_SECONDS,
            max(0.0, explicit_wait),
        )
    conn = get_connection()
    in_tx = getattr(_LOCAL, "in_transaction", False)
    if in_tx:
        depth = max(1, int(getattr(_LOCAL, "transaction_depth", 1)))
        savepoint = f"vector_lake_nested_{depth}"
        _LOCAL.transaction_depth = depth + 1
        conn.execute(f"SAVEPOINT {savepoint}")
        generation_snapshot = (
            conn.generation_dirty_snapshot()
            if isinstance(conn, _GenerationTrackingConnection)
            else None
        )
        try:
            yield conn
        except BaseException:
            try:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            finally:
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                if generation_snapshot is not None:
                    conn.restore_generation_dirty_snapshot(generation_snapshot)
            raise
        else:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        finally:
            _LOCAL.transaction_depth = depth
        return

    effective_wait = (
        _configured_transaction_max_wait_seconds()
        if explicit_wait is None
        else explicit_wait
    )
    deadline = time.monotonic() + effective_wait
    row = conn.execute("PRAGMA busy_timeout").fetchone()
    previous_busy_timeout = int(row[0]) if row is not None else 30_000

    def restore_busy_timeout() -> None:
        try:
            conn.execute(f"PRAGMA busy_timeout = {previous_busy_timeout}")
        except sqlite3.Error:
            pass

    max_retries = 60
    try:
        for attempt in range(max_retries):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("SQLite write transaction deadline exceeded")
            busy_timeout_ms = max(
                1,
                min(previous_busy_timeout, int(remaining * 1_000)),
            )
            conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                if "database is locked" in str(exc) and attempt < max_retries - 1:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(
                            "SQLite write transaction deadline exceeded"
                        ) from exc
                    time.sleep(min(0.05, remaining))
                    continue
                raise
    except BaseException:
        restore_busy_timeout()
        raise

    _LOCAL.in_transaction = True
    _LOCAL.transaction_depth = 1
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    finally:
        _LOCAL.in_transaction = False
        restore_busy_timeout()
        _LOCAL.transaction_depth = 0
        if getattr(_LOCAL, "close_when_transaction_ends", False):
            close_connection()


_INIT_DB_DONE = False
_INIT_LOCK = threading.Lock()


def init_db():
    db_path = get_db_path()
    db_key = str(db_path.resolve())
    if db_key in _INITIALIZED_DB_PATHS and db_path.exists():
        _validate_cached_identity_state(get_connection())
        return
    with _INIT_LOCK:
        if db_key in _INITIALIZED_DB_PATHS and db_path.exists():
            _validate_cached_identity_state(get_connection())
            return
        _INITIALIZED_DB_PATHS.discard(db_key)
        _init_db_once(db_key)


def _add_column_if_missing(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_type: str,
) -> None:
    """Apply a legacy column migration while surfacing every non-duplicate error."""
    try:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).casefold():
            raise


def _migrate_ingest_task_cleanup_schema_v4(conn: sqlite3.Connection) -> None:
    """Add every recoverable cleanup column and index without rebuilding the table."""
    conn.execute(_INGEST_TASK_CLEANUP_TABLE_SCHEMA_V4)
    observed = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info('ingest_task_cleanup')").fetchall()
    }
    missing_core = sorted(_INGEST_TASK_CLEANUP_CORE_COLUMNS_V4 - observed)
    if missing_core:
        raise RuntimeError(
            "Schema v4 cannot recover ingest_task_cleanup without core columns: "
            + ", ".join(missing_core)
        )
    for column_name, column_type in _INGEST_TASK_CLEANUP_ADD_COLUMNS_V4:
        if column_name not in observed:
            _add_column_if_missing(
                conn,
                "ingest_task_cleanup",
                column_name,
                column_type,
            )
            observed.add(column_name)

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE ingest_task_cleanup SET "
        "status = COALESCE(NULLIF(TRIM(status), ''), 'pending'), "
        "attempt_count = COALESCE(attempt_count, 0), "
        "lease_generation = COALESCE(lease_generation, 0), "
        "created_at = COALESCE(NULLIF(TRIM(created_at), ''), "
        "NULLIF(TRIM(updated_at), ''), NULLIF(TRIM(available_at), ''), ?), "
        "updated_at = COALESCE(NULLIF(TRIM(updated_at), ''), "
        "NULLIF(TRIM(created_at), ''), NULLIF(TRIM(available_at), ''), ?), "
        "available_at = COALESCE(NULLIF(TRIM(available_at), ''), "
        "NULLIF(TRIM(updated_at), ''), NULLIF(TRIM(created_at), ''), ?)",
        (now, now, now),
    )
    identity_rows = conn.execute(
        "SELECT cleanup_id, task_packet_path FROM ingest_task_cleanup "
        "WHERE expected_task_id IS NULL OR TRIM(expected_task_id) = ''"
    ).fetchall()
    identity_backfill = []
    for row in identity_rows:
        packet_value = str(row["task_packet_path"] or "").replace("\\", "/")
        task_id = Path(packet_value).stem
        if not task_id:
            raise RuntimeError(
                "Schema v4 cannot derive expected_task_id for cleanup row "
                f"{row['cleanup_id']}"
            )
        identity_backfill.append((task_id, int(row["cleanup_id"])))
    if identity_backfill:
        conn.executemany(
            "UPDATE ingest_task_cleanup SET expected_task_id = ? WHERE cleanup_id = ?",
            identity_backfill,
        )
    invalid_identity = conn.execute(
        "SELECT cleanup_id FROM ingest_task_cleanup "
        "WHERE job_id IS NULL OR TRIM(job_id) = '' "
        "OR task_packet_path IS NULL OR TRIM(task_packet_path) = '' "
        "OR expected_task_id IS NULL OR TRIM(expected_task_id) = '' LIMIT 1"
    ).fetchone()
    if invalid_identity is not None:
        raise RuntimeError(
            "Schema v4 ingest_task_cleanup identity backfill is incomplete at row "
            f"{invalid_identity['cleanup_id']}"
        )

    if not _ingest_task_cleanup_has_identity_index(conn):
        identity_index = conn.execute(
            "SELECT type, sql FROM sqlite_master "
            "WHERE name = 'idx_ingest_task_cleanup_identity_v4'"
        ).fetchone()
        if identity_index is not None:
            if str(identity_index["type"] or "") != "index":
                raise RuntimeError(
                    "Schema v4 identity index name is owned by a non-index object"
                )
            conn.execute("DROP INDEX idx_ingest_task_cleanup_identity_v4")
        conn.execute(_INGEST_TASK_CLEANUP_IDENTITY_INDEX_SCHEMA_V4)

    ready_index = conn.execute(
        "SELECT type, sql FROM sqlite_master "
        "WHERE name = 'idx_ingest_task_cleanup_ready'"
    ).fetchone()
    if ready_index is not None:
        if str(ready_index["type"] or "") != "index":
            raise RuntimeError(
                "Schema v4 cleanup ready index name is owned by a non-index object"
            )
        if _normalized_schema_sql(ready_index["sql"]) != _normalized_schema_sql(
            _INGEST_TASK_CLEANUP_READY_INDEX_SCHEMA_V4
        ):
            conn.execute("DROP INDEX idx_ingest_task_cleanup_ready")
    conn.execute(_INGEST_TASK_CLEANUP_READY_INDEX_SCHEMA_V4)

    issues = _ingest_task_cleanup_schema_issues(conn)
    if issues:
        raise RuntimeError(
            "Schema v4 ingest task cleanup migration failed: " + ", ".join(issues)
        )


def _validate_schema_migration_state(conn: sqlite3.Connection) -> int:
    """Create and validate the durable ledger inside the migration transaction."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, "
        "applied_at TEXT NOT NULL)"
    )
    row = conn.execute("PRAGMA user_version").fetchone()
    current_version = int(row[0] or 0)
    if current_version > _SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current_version} is newer than supported "
            f"version {_SCHEMA_VERSION}"
        )
    rows = conn.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    ledger = {
        int(item["version"]): (str(item["name"]), str(item["checksum"]))
        for item in rows
    }
    expected_versions = set(range(1, current_version + 1))
    if set(ledger) != expected_versions:
        raise RuntimeError(
            "schema_migrations ledger does not match PRAGMA user_version"
        )
    for version in expected_versions:
        expected = _SCHEMA_MIGRATIONS.get(version)
        if expected is None or ledger[version] != expected:
            raise RuntimeError(f"Schema migration ledger mismatch at version {version}")
    return current_version


def _record_schema_migrations(
    conn: sqlite3.Connection,
    current_version: int,
) -> None:
    """Commit migration identities and user_version with the schema changes."""
    applied_at = datetime.now(timezone.utc).isoformat()
    for version in range(current_version + 1, _SCHEMA_VERSION + 1):
        name, checksum = _SCHEMA_MIGRATIONS[version]
        conn.execute(
            "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
            "VALUES (?, ?, ?, ?)",
            (version, name, checksum, applied_at),
        )
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")


_CANONICAL_IDENTITY_SPECS = (
    ("claim", "claims", "claim_versions", "claim_id"),
    ("evidence", "evidence", "evidence_versions", "evidence_id"),
)


def _normalized_identity_page(value: object) -> str:
    page_key = os.path.basename(str(value or "").strip())
    return page_key[:-3] if page_key.casefold().endswith(".md") else page_key


def _identity_page_from_record(
    data_json: object,
    *,
    record_kind: str,
    record_id: str,
    version_page_key: object | None = None,
) -> str:
    try:
        record = json.loads(str(data_json))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Cannot migrate {record_kind}_id {record_id!r}: invalid identity JSON"
        ) from exc
    if not isinstance(record, dict):
        raise RuntimeError(
            f"Cannot migrate {record_kind}_id {record_id!r}: identity JSON is not an object"
        )
    id_field = f"{record_kind}_id"
    embedded_id = str(record.get(id_field) or "").strip()
    if embedded_id and embedded_id != record_id:
        raise RuntimeError(
            f"Cannot migrate {record_kind}_id {record_id!r}: payload owns {embedded_id!r}"
        )
    locator = record.get("locator")
    if not isinstance(locator, dict):
        raise RuntimeError(
            f"Cannot migrate {record_kind}_id {record_id!r}: missing locator owner"
        )
    page_key = _normalized_identity_page(locator.get("page_key"))
    if not page_key:
        raise RuntimeError(
            f"Cannot migrate {record_kind}_id {record_id!r}: missing page_key owner"
        )
    if version_page_key is not None:
        stored_page = _normalized_identity_page(version_page_key)
        if not stored_page or stored_page != page_key:
            raise RuntimeError(
                f"Cannot migrate {record_kind}_id {record_id!r}: version page ownership conflicts"
            )
    return page_key


def _validate_identity_registry_row(row: sqlite3.Row) -> str:
    record_kind = str(row["record_kind"] or "").strip()
    record_id = str(row["record_id"] or "").strip()
    page_key = _normalized_identity_page(row["page_key"])
    if record_kind not in {"claim", "evidence"} or not record_id or not page_key:
        raise RuntimeError("Canonical identity registry contains an invalid owner row")
    if str(row["identity_origin"] or "").strip() == "":
        raise RuntimeError(
            f"Canonical {record_kind}_id {record_id!r} has no identity origin"
        )
    try:
        payload = json.loads(str(row["data_json"]))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Canonical {record_kind}_id {record_id!r} has invalid registry JSON"
        ) from exc
    expected = {
        "record_kind": record_kind,
        "record_id": record_id,
        "page_key": page_key,
    }
    if not isinstance(payload, dict) or payload != expected:
        raise RuntimeError(
            f"Canonical {record_kind}_id {record_id!r} has conflicting registry metadata"
        )
    return page_key


def _reserve_migrated_identity(
    conn: sqlite3.Connection,
    *,
    record_kind: str,
    record_id: str,
    page_key: str,
) -> None:
    payload = json.dumps(
        {"record_kind": record_kind, "record_id": record_id, "page_key": page_key},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    row = conn.execute(
        "SELECT record_kind, record_id, page_key, identity_origin, data_json "
        "FROM canonical_identities WHERE record_kind = ? AND record_id = ?",
        (record_kind, record_id),
    ).fetchone()
    if row is not None:
        if _validate_identity_registry_row(row) != page_key:
            raise RuntimeError(
                f"Canonical {record_kind}_id {record_id!r} "
                "has conflicting page ownership"
            )
        return
    conn.execute(
        "INSERT INTO canonical_identities "
        "(record_kind, record_id, page_key, identity_origin, data_json, recorded_at) "
        "VALUES (?, ?, ?, 'schema_v2_backfill', ?, ?)",
        (
            record_kind,
            record_id,
            page_key,
            payload,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def _backfill_canonical_identities(conn: sqlite3.Connection) -> None:
    """Reserve every legacy current/version ID before schema v2 is committed."""
    for (
        record_kind,
        current_table,
        version_table,
        id_field,
    ) in _CANONICAL_IDENTITY_SPECS:
        rows = conn.execute(
            f"SELECT {id_field} AS record_id, data_json, NULL AS version_page_key "
            f"FROM {current_table} UNION ALL "
            f"SELECT {id_field} AS record_id, data_json, page_key AS version_page_key "
            f"FROM {version_table}"
        )
        for row in rows:
            record_id = str(row["record_id"] or "").strip()
            if not record_id:
                raise RuntimeError(
                    f"Cannot migrate {record_kind} identity without an ID"
                )
            page_key = _identity_page_from_record(
                row["data_json"],
                record_kind=record_kind,
                record_id=record_id,
                version_page_key=row["version_page_key"],
            )
            _reserve_migrated_identity(
                conn,
                record_kind=record_kind,
                record_id=record_id,
                page_key=page_key,
            )


def _validate_canonical_identity_registry(conn: sqlite3.Connection) -> None:
    for row in conn.execute(
        "SELECT record_kind, record_id, page_key, identity_origin, data_json "
        "FROM canonical_identities"
    ):
        _validate_identity_registry_row(row)


def _validate_canonical_identity_coverage(conn: sqlite3.Connection) -> None:
    """Stream canonical/history rows and require one matching durable owner."""
    for (
        record_kind,
        current_table,
        version_table,
        id_field,
    ) in _CANONICAL_IDENTITY_SPECS:
        rows = conn.execute(
            f"SELECT {id_field} AS record_id, data_json, NULL AS version_page_key "
            f"FROM {current_table} UNION ALL "
            f"SELECT {id_field} AS record_id, data_json, page_key AS version_page_key "
            f"FROM {version_table}"
        )
        for row in rows:
            record_id = str(row["record_id"] or "").strip()
            if not record_id:
                raise RuntimeError(
                    f"Schema v2 contains {record_kind} identity without an ID"
                )
            page_key = _identity_page_from_record(
                row["data_json"],
                record_kind=record_kind,
                record_id=record_id,
                version_page_key=row["version_page_key"],
            )
            identity = conn.execute(
                "SELECT record_kind, record_id, page_key, identity_origin, data_json "
                "FROM canonical_identities WHERE record_kind = ? AND record_id = ?",
                (record_kind, record_id),
            ).fetchone()
            if identity is None:
                raise RuntimeError(
                    f"Schema v2 {record_kind}_id {record_id!r} "
                    "is missing an identity registry owner"
                )
            registered_page = _validate_identity_registry_row(identity)
            if registered_page != page_key:
                raise RuntimeError(
                    f"Schema v2 {record_kind}_id {record_id!r} is owned by "
                    f"registry page {registered_page!r}, not {page_key!r}"
                )


def _init_operational_memory_search_schema(conn: sqlite3.Connection) -> bool:
    """Install the optional, lazily populated operational-memory search index.

    The index is contentless so it does not retain a second copy of every
    operational-memory document. SQLite versions without contentless-delete
    support keep the canonical table usable; callers transparently fall back to
    the compatibility scan.
    """
    enabled = os.environ.get("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "0")
    if str(enabled).strip().lower() not in {"1", "true", "yes", "on"}:
        for trigger_name in (
            "trg_operational_memory_search_insert",
            "trg_operational_memory_search_update",
            "trg_operational_memory_search_update_key",
            "trg_operational_memory_search_delete",
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
        return False
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS operational_memory_search_fts
        USING fts5(
            key_text,
            memory_text,
            page_text,
            type_text,
            content='',
            contentless_delete=1,
            tokenize='trigram case_sensitive 0'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS operational_memory_search_docs (
            doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL UNIQUE,
            source_updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS operational_memory_search_pending (
            memory_id TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            queued_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS operational_memory_search_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            backfill_cursor INTEGER NOT NULL DEFAULT 0,
            backfill_target INTEGER NOT NULL DEFAULT 0,
            schema_version INTEGER NOT NULL DEFAULT 3,
            updated_at TEXT
        )
    """)
    _add_column_if_missing(
        conn,
        "operational_memory_search_state",
        "schema_version",
        "INTEGER",
    )
    conn.execute(
        "INSERT OR IGNORE INTO operational_memory_search_state "
        "(singleton, backfill_cursor, backfill_target, schema_version, updated_at) "
        "SELECT 1, 0, COALESCE(MAX(rowid), 0), 3, ? FROM operational_memory",
        (datetime.now(timezone.utc).isoformat(),),
    )
    state = conn.execute(
        "SELECT schema_version FROM operational_memory_search_state WHERE singleton = 1"
    ).fetchone()
    if state is not None and int(state[0] or 0) != 3:
        conn.execute("DELETE FROM operational_memory_search_fts")
        conn.execute("DELETE FROM operational_memory_search_docs")
        conn.execute(
            "UPDATE operational_memory_search_state SET backfill_cursor = 0, "
            "backfill_target = (SELECT COALESCE(MAX(rowid), 0) "
            "FROM operational_memory), schema_version = 3, updated_at = ? "
            "WHERE singleton = 1",
            (datetime.now(timezone.utc).isoformat(),),
        )
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_operational_memory_search_insert
        AFTER INSERT ON operational_memory
        BEGIN
            INSERT INTO operational_memory_search_pending
                (memory_id, operation, queued_at)
            VALUES (NEW.memory_id, 'upsert', NEW.updated_at)
            ON CONFLICT(memory_id) DO UPDATE SET
                operation = 'upsert',
                queued_at = excluded.queued_at;
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_operational_memory_search_update
        AFTER UPDATE ON operational_memory
        BEGIN
            INSERT INTO operational_memory_search_pending
                (memory_id, operation, queued_at)
            VALUES (NEW.memory_id, 'upsert', NEW.updated_at)
            ON CONFLICT(memory_id) DO UPDATE SET
                operation = 'upsert',
                queued_at = excluded.queued_at;
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_operational_memory_search_update_key
        AFTER UPDATE OF memory_id ON operational_memory
        WHEN OLD.memory_id != NEW.memory_id
        BEGIN
            INSERT INTO operational_memory_search_pending
                (memory_id, operation, queued_at)
            VALUES (OLD.memory_id, 'delete', NEW.updated_at)
            ON CONFLICT(memory_id) DO UPDATE SET
                operation = 'delete',
                queued_at = excluded.queued_at;
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_operational_memory_search_delete
        AFTER DELETE ON operational_memory
        BEGIN
            INSERT INTO operational_memory_search_pending
                (memory_id, operation, queued_at)
            VALUES (OLD.memory_id, 'delete', OLD.updated_at)
            ON CONFLICT(memory_id) DO UPDATE SET
                operation = 'delete',
                queued_at = excluded.queued_at;
        END
    """)
    return True


def _init_db_once(db_key: str):
    conn = get_connection()
    in_tx = getattr(_LOCAL, "in_transaction", False)
    if not in_tx:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    with transaction():
        current_schema_version = _validate_schema_migration_state(conn)
        _assert_identity_schema_contract(conn)
        _assert_runtime_generation_schema_contract(conn)
        _assert_ingest_task_cleanup_schema_contract(conn)
        if current_schema_version >= 2:
            _validate_canonical_identity_registry(conn)
            _validate_canonical_identity_coverage(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                entity_id TEXT PRIMARY KEY,
                canonical_name TEXT,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_embeddings USING vec0(
                entity_id TEXT PRIMARY KEY,
                embedding float[3072]
            )
        """)
        for col, col_type in [
            ("type", "TEXT"),
            ("status", "TEXT"),
            ("ttl", "INTEGER"),
            ("decay_weight", "REAL"),
        ]:
            _add_column_if_missing(conn, "entities", col, col_type)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claims (
                claim_id TEXT PRIMARY KEY,
                claim_text TEXT,
                status TEXT,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS evidence (
                evidence_id TEXT PRIMARY KEY,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                source_id TEXT PRIMARY KEY,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS source_artifacts (
                artifact_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                sha256 TEXT,
                byte_size INTEGER,
                mime_type TEXT,
                storage_uri TEXT,
                integrity_status TEXT NOT NULL,
                data_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_source_artifacts_source "
            "ON source_artifacts(source_id, recorded_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS extraction_runs (
                run_id TEXT PRIMARY KEY,
                page_key TEXT NOT NULL,
                input_fingerprint TEXT NOT NULL,
                extractor_name TEXT NOT NULL,
                extractor_version TEXT NOT NULL,
                data_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_extraction_runs_page "
            "ON extraction_runs(page_key, recorded_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claim_versions (
                claim_version_id TEXT PRIMARY KEY,
                claim_id TEXT NOT NULL,
                claim_family_id TEXT NOT NULL,
                page_key TEXT,
                version_no INTEGER NOT NULL,
                record_hash TEXT NOT NULL,
                data_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                UNIQUE(claim_family_id, record_hash)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claim_versions_family "
            "ON claim_versions(claim_family_id, version_no)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS evidence_versions (
                evidence_version_id TEXT PRIMARY KEY,
                evidence_id TEXT NOT NULL,
                evidence_family_id TEXT NOT NULL,
                page_key TEXT,
                version_no INTEGER NOT NULL,
                record_hash TEXT NOT NULL,
                data_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                UNIQUE(evidence_family_id, record_hash)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_evidence_versions_family "
            "ON evidence_versions(evidence_family_id, version_no)"
        )
        if current_schema_version < 2:
            for statement in _CANONICAL_IDENTITIES_SCHEMA_V2:
                conn.execute(statement)
            _backfill_canonical_identities(conn)
            _validate_canonical_identity_registry(conn)
            _validate_canonical_identity_coverage(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS entity_identities (
                entity_id TEXT PRIMARY KEY,
                page_key TEXT NOT NULL UNIQUE,
                canonical_name TEXT,
                identity_origin TEXT NOT NULL,
                data_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claim_assessments (
                assessment_id TEXT PRIMARY KEY,
                claim_id TEXT NOT NULL,
                assessment_type TEXT NOT NULL,
                outcome TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                method_version TEXT NOT NULL,
                data_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claim_assessments_claim "
            "ON claim_assessments(claim_id, recorded_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS critical_decision_registry (
                decision_id TEXT PRIMARY KEY,
                registry_version TEXT NOT NULL,
                status TEXT NOT NULL,
                risk_weight REAL NOT NULL,
                verification TEXT NOT NULL,
                data_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_registry (
                schema_id TEXT NOT NULL,
                version TEXT NOT NULL,
                dialect_id TEXT NOT NULL,
                status TEXT NOT NULL,
                schema_hash TEXT NOT NULL,
                data_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (schema_id, version)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS quality_evaluation_runs (
                evaluation_id TEXT PRIMARY KEY,
                dataset_id TEXT NOT NULL,
                dataset_version TEXT NOT NULL,
                evaluator_version TEXT NOT NULL,
                status TEXT NOT NULL,
                data_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_quality_runs_dataset "
            "ON quality_evaluation_runs(dataset_id, dataset_version, recorded_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS change_sets (
                change_set_id TEXT PRIMARY KEY,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS change_set_idempotency (
                idempotency_key TEXT PRIMARY KEY,
                change_set_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS governance_queue (
                item_id TEXT PRIMARY KEY,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS merge_journal (
                journal_id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                status TEXT NOT NULL,
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_merge_journal_item "
            "ON merge_journal(item_id, updated_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mutation_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT,
                mutation_type TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            )
        """)
        outbox_columns = [
            ("payload_text", "TEXT"),
            ("attempt_count", "INTEGER DEFAULT 0"),
            ("last_error", "TEXT"),
            ("available_at", "TEXT"),
            ("started_at", "TEXT"),
            ("completed_at", "TEXT"),
            ("lease_until", "TEXT"),
            ("lease_owner", "TEXT"),
            ("lease_token", "TEXT"),
            ("lease_generation", "INTEGER DEFAULT 0"),
            ("superseded_by", "INTEGER"),
            ("idempotency_key", "TEXT"),
            ("validation_mode", "TEXT DEFAULT 'full'"),
            ("base_version", "TEXT"),
            ("projection_base_hash", "TEXT"),
        ]
        for column_name, column_type in outbox_columns:
            _add_column_if_missing(
                conn,
                "mutation_outbox",
                column_name,
                column_type,
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mutation_outbox_filename_status "
            "ON mutation_outbox(filename, status, id DESC)"
        )
        conn.execute(
            "UPDATE mutation_outbox SET available_at = created_at "
            "WHERE available_at IS NULL"
        )
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE mutation_outbox SET status = 'superseded', "
            "superseded_by = ("
            "  SELECT MAX(newer.id) FROM mutation_outbox AS newer "
            "  WHERE newer.filename = mutation_outbox.filename "
            "    AND newer.status IN ('pending', 'processing') "
            "    AND newer.id > mutation_outbox.id"
            "), completed_at = COALESCE(completed_at, ?), lease_until = NULL, "
            "lease_owner = NULL, lease_token = NULL "
            "WHERE status IN ('pending', 'processing') AND EXISTS ("
            "  SELECT 1 FROM mutation_outbox AS newer "
            "  WHERE newer.filename = mutation_outbox.filename "
            "    AND newer.status IN ('pending', 'processing') "
            "    AND newer.id > mutation_outbox.id"
            ")",
            (now,),
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mutation_outbox_ready "
            "ON mutation_outbox(status, available_at, lease_until, id)"
        )
        conn.execute("DROP INDEX IF EXISTS idx_mutation_outbox_idempotency")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mutation_outbox_idempotency_lookup "
            "ON mutation_outbox(idempotency_key, id DESC) WHERE idempotency_key IS NOT NULL"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mutation_outbox_filename_status "
            "ON mutation_outbox(filename, status, id DESC)"
        )
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS wiki_search_index USING fts5(
                node_key, title, summary, text,
                tokenize='unicode61 remove_diacritics 1'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alias_registry (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS operational_memory (
                memory_id TEXT PRIMARY KEY,
                memory_type TEXT,
                score REAL,
                status TEXT,
                ttl REAL,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        _add_column_if_missing(conn, "operational_memory", "status", "TEXT")
        _add_column_if_missing(conn, "operational_memory", "ttl", "REAL")
        _init_operational_memory_search_schema(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claim_graph_nodes (
                node_id TEXT PRIMARY KEY,
                data_json TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS claim_graph_edges (
                source_id TEXT,
                target_id TEXT,
                relation TEXT,
                weight REAL,
                updated_at TEXT,
                PRIMARY KEY (source_id, target_id, relation)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS page_graph_edges (
                source_id TEXT,
                target_id TEXT,
                relation TEXT,
                weight REAL,
                updated_at TEXT,
                PRIMARY KEY (source_id, target_id, relation)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS timeline_events (
                id TEXT PRIMARY KEY,
                event_date TEXT,
                action TEXT,
                sentiment TEXT,
                description TEXT,
                entity_id TEXT,
                entity_title TEXT,
                source_file TEXT,
                extracted_at TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_timeline_date ON timeline_events(event_date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_timeline_entity ON timeline_events(entity_id)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_files (
                filepath TEXT PRIMARY KEY,
                file_hash TEXT,
                processed_at TEXT,
                observed_mtime_ns INTEGER,
                observed_size INTEGER
            )
        """)
        _add_column_if_missing(conn, "processed_files", "observed_mtime_ns", "INTEGER")
        _add_column_if_missing(conn, "processed_files", "observed_size", "INTEGER")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS embedding_runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                model TEXT NOT NULL,
                candidates INTEGER DEFAULT 0,
                processed INTEGER DEFAULT 0,
                failed_batches INTEGER DEFAULT 0,
                last_error TEXT,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_embedding_runs_status "
            "ON embedding_runs(status, updated_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS embedding_rate_reservations (
                reservation_id TEXT PRIMARY KEY,
                reserved_at REAL NOT NULL,
                token_count INTEGER NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_embedding_rate_window "
            "ON embedding_rate_reservations(reserved_at)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                task_type TEXT,
                payload TEXT,
                status TEXT,
                retries INTEGER DEFAULT 0,
                error_msg TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        for column_name, column_type in [
            ("available_at", "TEXT"),
            ("lease_until", "TEXT"),
            ("lease_owner", "TEXT"),
            ("lease_token", "TEXT"),
            ("lease_generation", "INTEGER DEFAULT 0"),
            ("idempotency_key", "TEXT"),
            ("task_packet_path", "TEXT"),
            ("completed_at", "TEXT"),
            ("result_json", "TEXT"),
        ]:
            _add_column_if_missing(conn, "jobs", column_name, column_type)
        conn.execute(
            "UPDATE jobs SET lease_generation = 0 WHERE lease_generation IS NULL"
        )
        conn.execute("UPDATE jobs SET retries = 0 WHERE retries IS NULL")
        conn.execute(
            "UPDATE jobs SET available_at = created_at WHERE available_at IS NULL"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_ready "
            "ON jobs(status, available_at, lease_until, created_at)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency "
            "ON jobs(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        ingest_filepath_index_sql = (
            "CREATE INDEX IF NOT EXISTS idx_jobs_ingest_filepath_status "
            "ON jobs(CASE WHEN json_valid(payload) "
            "THEN json_extract(payload, '$.filepath') END, status, retries) "
            "WHERE task_type = 'ingest'"
        )
        existing_ingest_filepath_index = conn.execute(
            "SELECT type, sql FROM sqlite_master "
            "WHERE name = 'idx_jobs_ingest_filepath_status'"
        ).fetchone()
        if existing_ingest_filepath_index is not None and (
            str(existing_ingest_filepath_index["type"] or "") != "index"
            or _normalized_schema_sql(existing_ingest_filepath_index["sql"])
            != _normalized_schema_sql(ingest_filepath_index_sql)
        ):
            conn.execute("DROP INDEX idx_jobs_ingest_filepath_status")
        conn.execute(ingest_filepath_index_sql)
        if current_schema_version < 4:
            _migrate_ingest_task_cleanup_schema_v4(conn)

        conn.execute(_RUNTIME_GENERATIONS_TABLE_SCHEMA_V3)
        for surface in sorted(_RUNTIME_GENERATION_SURFACES):
            conn.execute(
                "INSERT OR IGNORE INTO runtime_generations (surface, generation) "
                "VALUES (?, 0)",
                (surface,),
            )
        if current_schema_version < 3:
            for surface in sorted(_RUNTIME_GENERATION_SURFACES):
                for operation_kind in ("insert", "update", "delete"):
                    conn.execute(
                        f"DROP TRIGGER IF EXISTS "
                        f"trg_{surface}_generation_v1_{operation_kind}"
                    )
                    conn.execute(
                        f"DROP TRIGGER IF EXISTS "
                        f"{_runtime_generation_trigger_name(surface, operation_kind)}"
                    )
            for statement in _RUNTIME_GENERATION_TRIGGER_SCHEMA_V3:
                conn.execute(statement)
            issues = _runtime_generation_schema_issues(conn)
            if issues:
                raise RuntimeError(
                    "Schema v3 runtime generation migration failed: "
                    + ", ".join(issues)
                )

        # Expression-index failures are migration failures and must roll back.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entities_type ON entities (json_extract(data_json, '$.type'))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entities_status ON entities (json_extract(data_json, '$.status'))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entities_page_key ON entities (json_extract(data_json, '$.page_key'))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_claims_page_key ON claims (json_extract(data_json, '$.locator.page_key'))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_evidence_page_key ON evidence (json_extract(data_json, '$.locator.page_key'))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_type ON operational_memory (json_extract(data_json, '$.memory_type'))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_status ON operational_memory (json_extract(data_json, '$.status'))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_source_claim ON operational_memory (json_extract(data_json, '$.source_claim_id'))"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_key ON operational_memory (memory_type, json_extract(data_json, '$.memory_key'))"
        )
        _record_schema_migrations(conn, current_schema_version)
    _assert_runtime_generation_schema_contract(conn)
    _assert_ingest_task_cleanup_schema_contract(conn)
    _INITIALIZED_DB_PATHS.add(db_key)
    _IDENTITY_VALIDATION_TOKENS[id(conn)] = _identity_validation_token(conn)


def upsert_search_index(node_key: str, title: str, summary: str, text: str):
    try:
        from vector_lake.tokenizer_runtime import tokenize_for_fts

        title_tok = tokenize_for_fts(title)
        summary_tok = tokenize_for_fts(summary)
        text_tok = tokenize_for_fts(text)
    except ImportError:
        title_tok = title if title else ""
        summary_tok = summary if summary else ""
        text_tok = text if text else ""

    conn = get_connection()
    with transaction():
        # FTS5 doesn't support ON CONFLICT REPLACE directly, so we delete then insert.
        conn.execute("DELETE FROM wiki_search_index WHERE node_key = ?", (node_key,))
        conn.execute(
            """
            INSERT INTO wiki_search_index (node_key, title, summary, text)
            VALUES (?, ?, ?, ?)
        """,
            (node_key, title_tok, summary_tok, text_tok),
        )


def upsert_embedding(entity_id: str, embedding: list[float]):
    if not embedding:
        return
    import math

    norm = math.sqrt(sum(x * x for x in embedding))
    if norm > 0:
        embedding = [x / norm for x in embedding]
    conn = get_connection()
    import sqlite_vec

    query_blob = sqlite_vec.serialize_float32(embedding)
    with transaction():
        conn.execute("DELETE FROM vec_embeddings WHERE entity_id = ?", (entity_id,))
        conn.execute(
            "INSERT INTO vec_embeddings (entity_id, embedding) VALUES (?, ?)",
            (entity_id, query_blob),
        )


def delete_embedding(entity_id: str):
    conn = get_connection()
    with transaction():
        conn.execute(
            "DELETE FROM vec_embeddings WHERE entity_id = ?", (str(entity_id),)
        )


def delete_stale_embeddings(valid_entity_ids: set[str]) -> int:
    conn = get_connection()
    valid = {str(item) for item in valid_entity_ids if item}
    rows = conn.execute("SELECT entity_id FROM vec_embeddings").fetchall()
    stale = [row["entity_id"] for row in rows if row["entity_id"] not in valid]
    if not stale:
        return 0
    with transaction():
        conn.executemany(
            "DELETE FROM vec_embeddings WHERE entity_id = ?",
            [(entity_id,) for entity_id in stale],
        )
    return len(stale)


def count_embeddings() -> int:
    conn = get_connection()
    return int(conn.execute("SELECT COUNT(*) FROM vec_embeddings").fetchone()[0])


def start_embedding_run(run_id: str, model: str, candidates: int):
    import os

    now = datetime.now(timezone.utc).isoformat()
    stale_after = max(
        60, int(os.environ.get("VECTOR_LAKE_EMBEDDING_RUN_STALE_SECONDS", "3600"))
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_after)).isoformat()
    with transaction():
        conn = get_connection()
        conn.execute(
            "UPDATE embedding_runs SET status = 'abandoned', completed_at = ?, updated_at = ?, "
            "last_error = 'Previous embedding process stopped without finalizing the run' "
            "WHERE status = 'running' AND updated_at < ?",
            (now, now, cutoff),
        )
        conn.execute(
            "INSERT INTO embedding_runs "
            "(run_id, status, model, candidates, processed, failed_batches, started_at, updated_at) "
            "VALUES (?, 'running', ?, ?, 0, 0, ?, ?)",
            (run_id, model, int(candidates), now, now),
        )


def update_embedding_run(
    run_id: str, processed: int, failed_batches: int = 0, last_error: str = ""
):
    now = datetime.now(timezone.utc).isoformat()
    with transaction():
        get_connection().execute(
            "UPDATE embedding_runs SET processed = ?, failed_batches = ?, last_error = ?, updated_at = ? "
            "WHERE run_id = ?",
            (int(processed), int(failed_batches), str(last_error)[:2000], now, run_id),
        )


def finish_embedding_run(
    run_id: str,
    status: str,
    processed: int,
    failed_batches: int = 0,
    last_error: str = "",
):
    if status not in {"completed", "failed", "partial"}:
        raise ValueError(f"Unsupported embedding run status: {status}")
    now = datetime.now(timezone.utc).isoformat()
    with transaction():
        get_connection().execute(
            "UPDATE embedding_runs SET status = ?, processed = ?, failed_batches = ?, "
            "last_error = ?, updated_at = ?, completed_at = ? WHERE run_id = ?",
            (
                status,
                int(processed),
                int(failed_batches),
                str(last_error)[:2000],
                now,
                now,
                run_id,
            ),
        )


def delete_search_index(node_key: str):
    conn = get_connection()
    with transaction():
        conn.execute("DELETE FROM wiki_search_index WHERE node_key = ?", (node_key,))
        conn.execute("DELETE FROM vec_embeddings WHERE entity_id = ?", (node_key,))


def delete_node_cascade(node_key: str):
    conn = get_connection()
    with transaction():
        rows = conn.execute(
            "SELECT entity_id, canonical_name, data_json FROM entities "
            "WHERE entity_id = ? OR canonical_name = ? "
            "OR json_extract(data_json, '$.page_key') = ?",
            (node_key, node_key, node_key),
        ).fetchall()
        entity_ids = {row["entity_id"] for row in rows}
        related_ids = sorted(entity_ids | {node_key})
        placeholders = ",".join("?" for _ in related_ids)
        old_claim_rows = conn.execute(
            "SELECT claim_id, claim_text, data_json, updated_at FROM claims WHERE "
            "json_extract(data_json, '$.locator.page_key') = ? OR "
            "json_extract(data_json, '$.source_page') IN (?, ?)",
            (node_key, node_key, node_key + ".md"),
        ).fetchall()
        old_evidence_rows = conn.execute(
            "SELECT evidence_id, data_json, updated_at FROM evidence WHERE "
            "json_extract(data_json, '$.locator.page_key') = ?",
            (node_key,),
        ).fetchall()

        # Deleting a canonical page must also close every durable projection that
        # can otherwise make the removed content look current.  Keep the history,
        # but turn it into an explicit tombstone inside this same transaction.
        now = datetime.now(timezone.utc).isoformat()
        claim_ids = sorted({str(row["claim_id"]) for row in old_claim_rows})
        memory_params: list[str] = [node_key, node_key + ".md"]
        memory_where = "json_extract(data_json, '$.source_page') IN (?, ?)"
        if claim_ids:
            claim_placeholders = ",".join("?" for _ in claim_ids)
            memory_where += (
                " OR json_extract(data_json, '$.source_claim_id') "
                f"IN ({claim_placeholders})"
            )
            memory_params.extend(claim_ids)
        memory_rows = conn.execute(
            f"SELECT memory_id, data_json FROM operational_memory WHERE {memory_where}",
            memory_params,
        ).fetchall()
        for memory_row in memory_rows:
            try:
                memory = json.loads(memory_row["data_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                memory = {"memory_id": memory_row["memory_id"]}
            reasons = memory.get("validity_reasons") or []
            if not isinstance(reasons, list):
                reasons = [str(reasons)]
            if "source_claim_deleted" not in reasons:
                reasons.append("source_claim_deleted")
            memory.update(
                {
                    "status": "Archived",
                    "validity_state": "archived",
                    "validity_reasons": reasons,
                    "deleted_at": now,
                    "updated_at": now,
                }
            )
            conn.execute(
                "UPDATE operational_memory SET status = 'Archived', data_json = ?, updated_at = ? "
                "WHERE memory_id = ?",
                (json.dumps(memory, ensure_ascii=False), now, memory_row["memory_id"]),
            )

        from vector_lake.governance_store import _append_version_records

        deleted_claims = []
        for row in old_claim_rows:
            record = json.loads(row["data_json"] or "{}")
            record.setdefault("claim_id", row["claim_id"])
            record.update(
                {"status": "Archived", "lifecycle_state": "deleted", "deleted_at": now}
            )
            deleted_claims.append(record)
        deleted_evidence = []
        for row in old_evidence_rows:
            record = json.loads(row["data_json"] or "{}")
            record.setdefault("evidence_id", row["evidence_id"])
            record.update(
                {"status": "Archived", "lifecycle_state": "deleted", "deleted_at": now}
            )
            deleted_evidence.append(record)
        _append_version_records(
            "claim_versions",
            "claim_id",
            "claim_family_id",
            "claimfamily",
            "claim_version",
            deleted_claims,
        )
        _append_version_records(
            "evidence_versions",
            "evidence_id",
            "evidence_family_id",
            "evidencefamily",
            "evidence_version",
            deleted_evidence,
        )

        for entity_row in rows:
            try:
                identity = json.loads(entity_row["data_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                identity = {}
            identity.update(
                {
                    "entity_id": entity_row["entity_id"],
                    "page_key": node_key,
                    "canonical_name": entity_row["canonical_name"],
                    "lifecycle_state": "deleted",
                    "deleted_at": now,
                }
            )
            conn.execute(
                "INSERT INTO entity_identities "
                "(entity_id, page_key, canonical_name, identity_origin, data_json, recorded_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(entity_id) DO UPDATE SET "
                "canonical_name = excluded.canonical_name, data_json = excluded.data_json, "
                "updated_at = excluded.updated_at",
                (
                    entity_row["entity_id"],
                    node_key,
                    entity_row["canonical_name"],
                    str(identity.get("identity_origin") or "deletion_backfill"),
                    json.dumps(identity, ensure_ascii=False),
                    now,
                    now,
                ),
            )

        conn.execute("DELETE FROM wiki_search_index WHERE node_key = ?", (node_key,))
        conn.execute(
            f"DELETE FROM vec_embeddings WHERE entity_id IN ({placeholders})",
            related_ids,
        )
        conn.execute(
            "DELETE FROM claims WHERE "
            "json_extract(data_json, '$.locator.page_key') = ? OR "
            "json_extract(data_json, '$.source_page') IN (?, ?)",
            (node_key, node_key, node_key + ".md"),
        )
        conn.execute(
            "DELETE FROM evidence WHERE json_extract(data_json, '$.locator.page_key') = ?",
            (node_key,),
        )
        conn.execute(
            "DELETE FROM sources WHERE source_id = ? OR "
            "json_extract(data_json, '$.canonical_source_page') = ?",
            (node_key, node_key + ".md"),
        )
        conn.execute(
            f"DELETE FROM alias_registry WHERE key = ? OR value IN ({placeholders})",
            [node_key, *related_ids],
        )
        conn.execute(
            f"DELETE FROM claim_graph_nodes WHERE node_id IN ({placeholders})",
            related_ids,
        )
        conn.execute(
            f"DELETE FROM claim_graph_edges WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
            [*related_ids, *related_ids],
        )
        from vector_lake.tool_timeline import sync_timeline_events_for_claim_delta

        sync_timeline_events_for_claim_delta(old_claim_rows, [])
        conn.execute(
            f"DELETE FROM entities WHERE entity_id IN ({placeholders})", related_ids
        )

    return {"page_key": node_key, "entity_ids": sorted(entity_ids)}


def enqueue_mutation(
    filename: str,
    mutation_type: str,
    payload_text: str | None = None,
    idempotency_key: str | None = None,
    validation_mode: str = "full",
    base_version: str | None = None,
    projection_base_hash: str | None = None,
) -> int:
    if mutation_type not in {"update", "delete"}:
        raise ValueError(f"Unsupported mutation_type: {mutation_type}")
    if validation_mode not in {"full", "schema"}:
        raise ValueError(f"Unsupported validation_mode: {validation_mode}")
    init_db()
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    with transaction():
        current_id = None
        if idempotency_key:
            existing = conn.execute(
                "SELECT id, status FROM mutation_outbox WHERE idempotency_key = ? "
                "ORDER BY id DESC LIMIT 1",
                (idempotency_key,),
            ).fetchone()
            if existing:
                newer = conn.execute(
                    "SELECT id FROM mutation_outbox WHERE filename = ? AND id > ? "
                    "AND status != 'superseded' ORDER BY id DESC LIMIT 1",
                    (filename, existing["id"]),
                ).fetchone()
                if newer is None:
                    if existing["status"] == "failed":
                        conn.execute(
                            "UPDATE mutation_outbox SET status = 'pending', attempt_count = 0, "
                            "last_error = NULL, available_at = ?, completed_at = NULL, "
                            "lease_until = NULL, lease_owner = NULL, lease_token = NULL, "
                            "superseded_by = NULL WHERE id = ?",
                            (now, existing["id"]),
                        )
                        current_id = int(existing["id"])
                    else:
                        return int(existing["id"])
        if current_id is None:
            cursor = conn.execute(
                "INSERT INTO mutation_outbox "
                "(filename, mutation_type, payload_text, status, attempt_count, created_at, available_at, "
                "idempotency_key, validation_mode, base_version, projection_base_hash) "
                "VALUES (?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?)",
                (
                    filename,
                    mutation_type,
                    payload_text,
                    now,
                    now,
                    idempotency_key,
                    validation_mode,
                    base_version,
                    projection_base_hash,
                ),
            )
            current_id = int(cursor.lastrowid)
        conn.execute(
            "UPDATE mutation_outbox SET status = 'superseded', superseded_by = ?, "
            "completed_at = COALESCE(completed_at, ?), lease_until = NULL, "
            "lease_owner = NULL, lease_token = NULL "
            "WHERE filename = ? AND id != ? AND status IN ('pending', 'processing')",
            (current_id, now, filename, current_id),
        )
        return current_id


def is_managed_projection_state(
    filename: str,
    mutation_type: str,
    payload_text: str | None = None,
) -> bool:
    """Return whether a filesystem event matches the latest durable projection intent."""
    init_db()
    row = (
        get_connection()
        .execute(
            "SELECT mutation_type, payload_text, status FROM mutation_outbox "
            "WHERE filename = ? AND status != 'superseded' ORDER BY id DESC LIMIT 1",
            (str(filename),),
        )
        .fetchone()
    )
    if row is None:
        return False
    if str(row["mutation_type"]) != str(mutation_type):
        return False
    if mutation_type == "delete":
        return True
    if row["payload_text"] is None or payload_text is None:
        return row["payload_text"] == payload_text
    return normalize_semantic_text(row["payload_text"]) == normalize_semantic_text(
        payload_text
    )


def claim_mutation_outbox(
    limit: int = 50,
    lease_seconds: int = 120,
    lease_owner: str | None = None,
    outbox_ids: list[int] | None = None,
) -> list[dict]:
    """Atomically claim ready rows, including abandoned processing leases."""
    import os
    import secrets
    import socket

    init_db()
    conn = get_connection()
    owner = (
        lease_owner
        or os.environ.get("VECTOR_LAKE_OUTBOX_RUN_ID")
        or f"{socket.gethostname()}:{os.getpid()}"
    )
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    lease_until = (now_dt + timedelta(seconds=max(1, lease_seconds))).isoformat()
    with transaction():
        conn.execute(
            "UPDATE mutation_outbox SET status = 'superseded', "
            "superseded_by = ("
            "  SELECT MAX(newer.id) FROM mutation_outbox AS newer "
            "  WHERE newer.filename = mutation_outbox.filename "
            "    AND newer.status IN ('pending', 'processing') "
            "    AND newer.id > mutation_outbox.id"
            "), completed_at = COALESCE(completed_at, ?), lease_until = NULL, "
            "lease_owner = NULL, lease_token = NULL "
            "WHERE status IN ('pending', 'processing') AND EXISTS ("
            "  SELECT 1 FROM mutation_outbox AS newer "
            "  WHERE newer.filename = mutation_outbox.filename "
            "    AND newer.status IN ('pending', 'processing') "
            "    AND newer.id > mutation_outbox.id"
            ")",
            (now,),
        )
        requested_ids = sorted({int(value) for value in (outbox_ids or [])})
        query = (
            "SELECT id FROM mutation_outbox WHERE ((status = 'pending' "
            "AND COALESCE(available_at, created_at, '') <= ?) OR "
            "(status = 'processing' AND COALESCE(lease_until, '') <= ?))"
        )
        params: list = [now, now]
        if requested_ids:
            placeholders = ",".join("?" for _ in requested_ids)
            query += f" AND id IN ({placeholders})"
            params.extend(requested_ids)
        query += " ORDER BY id ASC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = conn.execute(query, tuple(params)).fetchall()
        ids = [int(row["id"]) for row in rows]
        if not ids:
            return []
        claimed_ids = []
        for outbox_id in ids:
            token = secrets.token_hex(16)
            claimed = conn.execute(
                "UPDATE mutation_outbox SET status = 'processing', "
                "attempt_count = COALESCE(attempt_count, 0) + 1, started_at = ?, "
                "lease_until = ?, lease_owner = ?, lease_token = ?, "
                "lease_generation = COALESCE(lease_generation, 0) + 1 "
                "WHERE id = ? AND ((status = 'pending' AND COALESCE(available_at, created_at, '') <= ?) "
                "OR (status = 'processing' AND COALESCE(lease_until, '') <= ?))",
                (now, lease_until, owner, token, outbox_id, now, now),
            )
            if claimed.rowcount:
                claimed_ids.append(outbox_id)
        if not claimed_ids:
            return []
        placeholders = ",".join("?" for _ in claimed_ids)
        claimed = conn.execute(
            f"SELECT * FROM mutation_outbox WHERE id IN ({placeholders}) ORDER BY id ASC",
            claimed_ids,
        ).fetchall()
        return [dict(row) for row in claimed]


def mutation_outbox_lease_is_current(
    outbox_id: int,
    lease_owner: str,
    lease_token: str,
    lease_generation: int,
) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    row = (
        get_connection()
        .execute(
            "SELECT 1 FROM mutation_outbox WHERE id = ? AND status = 'processing' "
            "AND lease_owner = ? AND lease_token = ? AND lease_generation = ? "
            "AND COALESCE(lease_until, '') > ?",
            (outbox_id, lease_owner, lease_token, int(lease_generation), now),
        )
        .fetchone()
    )
    return row is not None


def mutation_outbox_is_latest_intent(outbox_id: int) -> bool:
    row = (
        get_connection()
        .execute(
            "SELECT 1 FROM mutation_outbox AS current WHERE current.id = ? "
            "AND current.status != 'superseded' AND NOT EXISTS ("
            "  SELECT 1 FROM mutation_outbox AS newer "
            "  WHERE newer.filename = current.filename AND newer.id > current.id "
            "    AND newer.status != 'superseded'"
            ")",
            (int(outbox_id),),
        )
        .fetchone()
    )
    return row is not None


def mutation_outbox_statuses(outbox_ids: list[int]) -> dict[int, str]:
    ids = sorted({int(value) for value in outbox_ids})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = (
        get_connection()
        .execute(
            f"SELECT id, status FROM mutation_outbox WHERE id IN ({placeholders})",
            ids,
        )
        .fetchall()
    )
    return {int(row["id"]): str(row["status"]) for row in rows}


def record_merge_journal(
    journal_id: str,
    item_id: str,
    data: dict,
    status: str = "prepared",
) -> dict:
    """Persist an immutable pre-merge snapshot inside the caller transaction."""
    now = datetime.now(timezone.utc).isoformat()
    payload = dict(data)
    payload.setdefault("journal_id", str(journal_id))
    payload.setdefault("item_id", str(item_id))
    payload.setdefault("created_at", now)
    with transaction():
        get_connection().execute(
            "INSERT OR IGNORE INTO merge_journal "
            "(journal_id, item_id, status, data_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(journal_id),
                str(item_id),
                str(status),
                json.dumps(payload, ensure_ascii=False),
                now,
                now,
            ),
        )
    return get_merge_journal(journal_id) or payload


def update_merge_journal(
    journal_id: str, updates: dict, status: str | None = None
) -> dict | None:
    """Update merge execution metadata without replacing the pre-merge snapshot."""
    with transaction():
        row = (
            get_connection()
            .execute(
                "SELECT status, data_json FROM merge_journal WHERE journal_id = ?",
                (str(journal_id),),
            )
            .fetchone()
        )
        if row is None:
            return None
        payload = json.loads(row["data_json"] or "{}")
        payload.update(dict(updates))
        next_status = str(status or row["status"])
        now = datetime.now(timezone.utc).isoformat()
        get_connection().execute(
            "UPDATE merge_journal SET status = ?, data_json = ?, updated_at = ? "
            "WHERE journal_id = ?",
            (
                next_status,
                json.dumps(payload, ensure_ascii=False),
                now,
                str(journal_id),
            ),
        )
    return {**payload, "status": next_status}


def get_merge_journal(journal_id: str) -> dict | None:
    row = (
        get_connection()
        .execute(
            "SELECT status, data_json, created_at, updated_at FROM merge_journal WHERE journal_id = ?",
            (str(journal_id),),
        )
        .fetchone()
    )
    if row is None:
        return None
    payload = json.loads(row["data_json"] or "{}")
    payload.update(
        {
            "status": str(row["status"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }
    )
    return payload


def complete_mutation_outbox(
    outbox_id: int,
    lease_owner: str,
    lease_token: str,
    lease_generation: int,
) -> bool:
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    with transaction():
        updated = conn.execute(
            "UPDATE mutation_outbox SET status = 'completed', completed_at = ?, "
            "lease_until = NULL, lease_owner = NULL, lease_token = NULL, last_error = NULL "
            "WHERE id = ? AND status = 'processing' AND lease_owner = ? "
            "AND lease_token = ? AND lease_generation = ? AND COALESCE(lease_until, '') > ?",
            (now, outbox_id, lease_owner, lease_token, int(lease_generation), now),
        )
    return bool(updated.rowcount)


def fail_mutation_outbox(
    outbox_id: int,
    error: str,
    lease_owner: str,
    lease_token: str,
    lease_generation: int,
    max_attempts: int = 3,
    backoff_base: float = 2.0,
) -> str:
    conn = get_connection()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    with transaction():
        row = conn.execute(
            "SELECT attempt_count FROM mutation_outbox WHERE id = ? AND status = 'processing' "
            "AND lease_owner = ? AND lease_token = ? AND lease_generation = ? "
            "AND COALESCE(lease_until, '') > ?",
            (outbox_id, lease_owner, lease_token, int(lease_generation), now),
        ).fetchone()
        if row is None:
            return "stale"
        attempts = int(row["attempt_count"] or 0)
        terminal = attempts >= max(1, int(max_attempts))
        status = "failed" if terminal else "pending"
        delay_seconds = (
            0.0
            if terminal
            else max(0.0, float(backoff_base)) * (2 ** max(0, attempts - 1))
        )
        available_at = (now_dt + timedelta(seconds=delay_seconds)).isoformat()
        updated = conn.execute(
            "UPDATE mutation_outbox SET status = ?, last_error = ?, available_at = ?, "
            "lease_until = NULL, lease_owner = NULL, lease_token = NULL WHERE id = ? "
            "AND status = 'processing' AND lease_owner = ? AND lease_token = ? "
            "AND lease_generation = ? AND COALESCE(lease_until, '') > ?",
            (
                status,
                str(error)[:4000],
                available_at,
                outbox_id,
                lease_owner,
                lease_token,
                int(lease_generation),
                now,
            ),
        )
        return status if updated.rowcount else "stale"


def search_wiki(query: str, limit: int = 50) -> list[dict]:
    import re

    query = re.sub(r"[^\w\s\u4e00-\u9fa5]", " ", query) if query else ""
    try:
        from vector_lake.tokenizer_runtime import tokenize_for_fts

        query_tok = tokenize_for_fts(query)
    except ImportError:
        query_tok = query if query else ""

    query_tok = query_tok.strip()
    if not query_tok:
        return []

    query_esc = " ".join(f'"{t}"' for t in query_tok.split())

    conn = get_connection()
    cur = conn.execute(
        """
        SELECT node_key, title, summary, bm25(wiki_search_index) as rank 
        FROM wiki_search_index 
        WHERE wiki_search_index MATCH ? 
        ORDER BY rank LIMIT ?
    """,
        (query_esc, limit),
    )
    return [dict(row) for row in cur.fetchall()]


def get_processed_files() -> dict[str, str]:
    conn = get_connection()
    cur = conn.execute("SELECT filepath, file_hash FROM processed_files")
    return {row["filepath"]: row["file_hash"] for row in cur.fetchall()}


def update_processed_file_observations(observations) -> int:
    """Refresh changed stat hints for unchanged processed revisions in one write."""
    rows = [
        (
            int(observed_mtime_ns),
            int(observed_size),
            str(filepath),
            str(file_hash),
            int(observed_mtime_ns),
            int(observed_size),
        )
        for filepath, file_hash, observed_mtime_ns, observed_size in observations
    ]
    if not rows:
        return 0
    conn = get_connection()
    with transaction():
        before = conn.total_changes
        conn.executemany(
            "UPDATE processed_files "
            "SET observed_mtime_ns = ?, observed_size = ? "
            "WHERE filepath = ? AND file_hash = ? "
            "AND (observed_mtime_ns IS NOT ? OR observed_size IS NOT ?)",
            rows,
        )
        changed = conn.total_changes - before
    return changed


def update_processed_file_observation(
    filepath: str,
    file_hash: str,
    observed_mtime_ns: int,
    observed_size: int,
) -> bool:
    """Refresh stat metadata only when the canonical processed hash still matches."""
    return (
        update_processed_file_observations(
            [(filepath, file_hash, observed_mtime_ns, observed_size)]
        )
        == 1
    )


def mark_file_processed(filepath: str, file_hash: str):
    from datetime import datetime, timezone

    conn = get_connection()
    now_str = datetime.now(timezone.utc).isoformat()
    # The finalized hash belongs to the dispatched raw snapshot. Do not bind
    # current filesystem metadata to it: the source may have changed in flight.
    observed_mtime_ns = None
    observed_size = None
    with transaction():
        conn.execute(
            """
            INSERT INTO processed_files (
                filepath, file_hash, processed_at, observed_mtime_ns, observed_size
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(filepath) DO UPDATE SET
                file_hash = excluded.file_hash,
                processed_at = excluded.processed_at,
                observed_mtime_ns = excluded.observed_mtime_ns,
                observed_size = excluded.observed_size
        """,
            (
                filepath,
                file_hash,
                now_str,
                observed_mtime_ns,
                observed_size,
            ),
        )


def _ingest_identity_owner_is_releasable(
    conn: sqlite3.Connection,
    owner,
    candidate_payload: dict,
) -> bool:
    """Apply one definition of terminal/recoverable ingest identity ownership."""
    if owner is None or not isinstance(candidate_payload, dict):
        return False
    record = dict(owner)
    if str(record.get("task_type") or "") != "ingest":
        return False
    status = str(record.get("status") or "")
    try:
        retries = int(record.get("retries") or 0)
    except (TypeError, ValueError):
        retries = 0
    if status in {"cancelled", "superseded"} or (status == "failed" and retries >= 3):
        return True
    if status not in {"completed", "finalized"}:
        return False
    try:
        previous_payload = json.loads(str(record.get("payload") or "{}"))
    except (TypeError, json.JSONDecodeError):
        return False
    same_revision = isinstance(previous_payload, dict) and all(
        str(previous_payload.get(field) or "")
        == str(candidate_payload.get(field) or "")
        for field in ("filepath", "hash", "canonical_name")
    )
    if not same_revision:
        return False
    processed = conn.execute(
        "SELECT file_hash FROM processed_files WHERE filepath = ?",
        (str(candidate_payload.get("filepath") or ""),),
    ).fetchone()
    return processed is None or str(processed["file_hash"] or "") != str(
        candidate_payload.get("hash") or ""
    )


def _release_releasable_ingest_identity_owner(
    conn: sqlite3.Connection,
    owner,
    idempotency_key: str,
    candidate_payload: dict,
    now: str,
) -> bool:
    """CAS-release only an owner proven terminal or no longer durable."""
    if not _ingest_identity_owner_is_releasable(conn, owner, candidate_payload):
        return False
    record = dict(owner)
    cursor = conn.execute(
        "UPDATE jobs SET idempotency_key = NULL, updated_at = ? "
        "WHERE job_id = ? AND task_type = 'ingest' AND idempotency_key = ? "
        "AND status IS ? AND retries IS ? AND payload IS ? AND updated_at IS ?",
        (
            str(now),
            str(record.get("job_id") or ""),
            str(idempotency_key),
            record.get("status"),
            record.get("retries"),
            record.get("payload"),
            record.get("updated_at"),
        ),
    )
    if cursor.rowcount != 1:
        raise RuntimeError("Ingest identity owner changed before CAS release")
    return True


def enqueue_job(
    task_type: str, payload: dict, idempotency_key: str | None = None
) -> str:
    import uuid
    from datetime import datetime, timezone

    init_db()
    conn = get_connection()
    job_id = uuid.uuid4().hex
    now_str = datetime.now(timezone.utc).isoformat()
    key = idempotency_key or _job_idempotency_key(task_type, payload)
    with transaction():
        if key:
            existing = conn.execute(
                "SELECT job_id, task_type, status, retries, payload, updated_at "
                "FROM jobs WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if existing and not _release_releasable_ingest_identity_owner(
                conn,
                existing,
                key,
                payload,
                now_str,
            ):
                return str(existing["job_id"])
        if task_type == "ingest" and isinstance(payload, dict):
            filepath = str(payload.get("filepath") or "")
            if filepath:
                superseded_rows = conn.execute(
                    "SELECT job_id, task_packet_path FROM jobs "
                    "WHERE task_type = 'ingest' "
                    "AND CASE WHEN json_valid(payload) "
                    "THEN json_extract(payload, '$.filepath') END = ? "
                    "AND (status IN ('queued', 'dispatched', 'awaiting_subagent', "
                    "'subagent_processing') OR "
                    "(status = 'failed' AND COALESCE(retries, 0) < 3))",
                    (filepath,),
                ).fetchall()
                if superseded_rows:
                    reason = (
                        "Superseded by newer raw source version "
                        f"before ingest completion: {job_id}"
                    )
                    result_json = json.dumps(
                        {"superseded_by": job_id},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    conn.execute(
                        "UPDATE jobs SET status = 'superseded', "
                        "idempotency_key = NULL, error_msg = ?, result_json = ?, "
                        "updated_at = ?, completed_at = ?, available_at = NULL, "
                        "lease_until = NULL, lease_owner = NULL, lease_token = NULL "
                        "WHERE task_type = 'ingest' "
                        "AND CASE WHEN json_valid(payload) "
                        "THEN json_extract(payload, '$.filepath') END = ? "
                        "AND (status IN ('queued', 'dispatched', 'awaiting_subagent', "
                        "'subagent_processing') OR "
                        "(status = 'failed' AND COALESCE(retries, 0) < 3))",
                        (reason, result_json, now_str, now_str, filepath),
                    )
                    for superseded in superseded_rows:
                        packet_path = str(superseded["task_packet_path"] or "").strip()
                        if packet_path:
                            enqueue_ingest_task_cleanup(
                                str(superseded["job_id"]),
                                packet_path,
                            )
        conn.execute(
            """
            INSERT INTO jobs (job_id, task_type, payload, status, created_at, updated_at, available_at, idempotency_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                job_id,
                task_type,
                json.dumps(payload, ensure_ascii=False),
                "queued",
                now_str,
                now_str,
                now_str,
                key,
            ),
        )
    return job_id


def _current_ingest_contract_sql(payload_column: str = "payload") -> str:
    """Return the shared SQLite predicate for a dispatchable ingest payload."""
    if payload_column != "payload":
        raise ValueError("Only the jobs.payload column is supported")
    return (
        "CASE WHEN json_valid(payload) = 0 THEN 0 "
        "WHEN COALESCE(json_type(payload), '') <> 'object' THEN 0 "
        "WHEN COALESCE(json_type(payload, '$.filepath'), '') <> 'text' THEN 0 "
        "WHEN COALESCE(TRIM(json_extract(payload, '$.filepath')), '') = '' THEN 0 "
        "WHEN COALESCE(json_type(payload, '$.hash'), '') <> 'text' THEN 0 "
        "WHEN COALESCE(TRIM(json_extract(payload, '$.hash')), '') = '' THEN 0 "
        "WHEN COALESCE(json_type(payload, '$.canonical_name'), '') <> 'text' THEN 0 "
        "WHEN COALESCE(TRIM(json_extract(payload, '$.canonical_name')), '') = '' THEN 0 "
        "WHEN COALESCE(json_type(payload, '$.instructions'), '') <> 'text' THEN 0 "
        "WHEN COALESCE(TRIM(json_extract(payload, '$.instructions')), '') = '' THEN 0 "
        "WHEN COALESCE(json_type(payload, '$.source_hash'), '') <> 'text' THEN 0 "
        "WHEN COALESCE(json_type(payload, '$.source_projection_hash'), '') <> 'text' "
        "THEN 0 "
        "WHEN COALESCE(json_type(payload, '$.integration_candidates'), '') "
        "<> 'array' THEN 0 "
        "WHEN COALESCE(CAST(json_extract("
        "payload, '$.ingest_contract_version') AS TEXT), '') <> ? THEN 0 "
        "ELSE 1 END = 1"
    )


def claim_pending_jobs(
    limit: int = 10,
    lease_seconds: int = 300,
    lease_owner: str | None = None,
    *,
    task_type: str | None = None,
    required_ingest_contract_version: int | None = None,
) -> list[dict]:
    """Fence dispatch work so an expired worker cannot publish a stale handoff."""
    import secrets
    import socket

    init_db()
    conn = get_connection()
    owner = str(
        lease_owner
        or os.environ.get("VECTOR_LAKE_INGEST_WORKER_RUN_ID")
        or f"{socket.gethostname()}:{os.getpid()}"
    )
    effective_task_type = None if task_type is None else str(task_type).strip()
    if task_type is not None and not effective_task_type:
        raise ValueError("task_type must be non-empty when supplied")
    contract_version = None
    if required_ingest_contract_version is not None:
        contract_version = int(required_ingest_contract_version)
        if contract_version < 1:
            raise ValueError("required_ingest_contract_version must be positive")
        if effective_task_type not in (None, "ingest"):
            raise ValueError("ingest contract filtering requires task_type='ingest'")
        effective_task_type = "ingest"

    scope_sql = ""
    scope_params: list[object] = []
    if effective_task_type is not None:
        scope_sql += " AND task_type = ?"
        scope_params.append(effective_task_type)
    if contract_version is not None:
        scope_sql += f" AND ({_current_ingest_contract_sql()})"
        scope_params.append(str(contract_version))

    with transaction():
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease_until = (
            now_dt + timedelta(seconds=max(1, int(lease_seconds)))
        ).isoformat()
        rows = conn.execute(
            "SELECT job_id FROM jobs WHERE "
            "((status IN ('queued', 'failed') AND COALESCE(retries, 0) < 3 "
            "AND COALESCE(available_at, created_at, '') <= ?) "
            "OR (status = 'dispatched' AND COALESCE(lease_until, '') <= ?))"
            + scope_sql
            + " ORDER BY created_at ASC LIMIT ?",
            (now, now, *scope_params, max(1, int(limit))),
        ).fetchall()
        claimed: list[dict] = []
        for row in rows:
            job_id = str(row["job_id"])
            token = secrets.token_urlsafe(32)
            cursor = conn.execute(
                "UPDATE jobs SET status = 'dispatched', lease_until = ?, "
                "lease_owner = ?, lease_token = ?, "
                "lease_generation = COALESCE(lease_generation, 0) + 1, "
                "updated_at = ? WHERE job_id = ? AND ("
                "(status IN ('queued', 'failed') "
                "AND COALESCE(retries, 0) < 3 "
                "AND COALESCE(available_at, created_at, '') <= ?) OR "
                "(status = 'dispatched' AND COALESCE(lease_until, '') <= ?))"
                + scope_sql,
                (
                    lease_until,
                    owner,
                    token,
                    now,
                    job_id,
                    now,
                    now,
                    *scope_params,
                ),
            )
            if cursor.rowcount != 1:
                continue
            claimed_row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if claimed_row is not None:
                claimed.append(dict(claimed_row))
        return claimed


def get_pending_jobs(limit: int = 10) -> list[dict]:
    conn = get_connection()
    cur = conn.execute(
        """
        SELECT * FROM jobs 
        WHERE status = 'queued'
        OR (status = 'failed' AND COALESCE(retries, 0) < 3)
        ORDER BY created_at ASC LIMIT ?
    """,
        (limit,),
    )
    return [dict(row) for row in cur.fetchall()]


def get_jobs_by_status(statuses: list[str], limit: int = 20) -> list[dict]:
    if not statuses:
        return []
    init_db()
    conn = get_connection()
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY created_at ASC LIMIT ?",
        [*statuses, max(1, int(limit))],
    ).fetchall()
    return [dict(row) for row in rows]


def _dispatch_lease_credentials(
    lease_owner: str | None,
    lease_token: str | None,
    lease_generation: int | None,
) -> tuple[str, str, int] | None:
    """Normalize an optional dispatch lease, rejecting partial credentials."""
    supplied = (
        lease_owner is not None,
        lease_token is not None,
        lease_generation is not None,
    )
    if not any(supplied):
        return None
    if not all(supplied):
        raise ValueError(
            "lease_owner, lease_token, and lease_generation are required together"
        )
    owner = str(lease_owner or "").strip()
    token = str(lease_token or "").strip()
    try:
        generation = int(lease_generation)
    except (TypeError, ValueError) as exc:
        raise ValueError("lease_generation must be a positive integer") from exc
    if not owner or not token or generation < 1:
        raise ValueError("dispatch lease credentials are invalid")
    return owner, token, generation


def renew_job_dispatch_lease(
    job_id: str,
    lease_owner: str,
    lease_token: str,
    lease_generation: int,
    lease_seconds: int = 300,
) -> bool:
    """Extend only the current, unexpired dispatch lease."""
    lease = _dispatch_lease_credentials(
        lease_owner,
        lease_token,
        lease_generation,
    )
    if lease is None:
        raise ValueError("dispatch lease credentials are required")
    owner, token, generation = lease
    with transaction():
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        lease_until = (
            now_dt + timedelta(seconds=max(1, int(lease_seconds)))
        ).isoformat()
        cursor = get_connection().execute(
            "UPDATE jobs SET lease_until = ?, updated_at = ? "
            "WHERE job_id = ? AND status = 'dispatched' "
            "AND lease_owner = ? AND lease_token = ? AND lease_generation = ? "
            "AND COALESCE(lease_until, '') > ?",
            (
                lease_until,
                now,
                str(job_id),
                owner,
                token,
                generation,
                now,
            ),
        )
        return cursor.rowcount == 1


def mark_job_awaiting_subagent(
    job_id: str,
    task_packet_path: str,
    *,
    lease_owner: str | None = None,
    lease_token: str | None = None,
    lease_generation: int | None = None,
) -> bool:
    """Publish a task packet only while the caller still owns dispatch."""
    conn = get_connection()
    next_packet_path = str(task_packet_path or "")
    lease = _dispatch_lease_credentials(
        lease_owner,
        lease_token,
        lease_generation,
    )
    with transaction():
        now_str = datetime.now(timezone.utc).isoformat()
        if lease is None:
            current = conn.execute(
                "SELECT task_packet_path, status FROM jobs WHERE job_id = ? "
                "AND status IN ('queued', 'failed', 'awaiting_subagent')",
                (str(job_id),),
            ).fetchone()
            predicate = (
                "job_id = ? AND status IN ('queued', 'failed', 'awaiting_subagent')"
            )
            predicate_params: tuple = (str(job_id),)
        else:
            owner, token, generation = lease
            current = conn.execute(
                "SELECT task_packet_path, status FROM jobs WHERE job_id = ? "
                "AND status = 'dispatched' AND lease_owner = ? "
                "AND lease_token = ? AND lease_generation = ? "
                "AND COALESCE(lease_until, '') > ?",
                (str(job_id), owner, token, generation, now_str),
            ).fetchone()
            predicate = (
                "job_id = ? AND status = 'dispatched' AND lease_owner = ? "
                "AND lease_token = ? AND lease_generation = ? "
                "AND COALESCE(lease_until, '') > ?"
            )
            predicate_params = (
                str(job_id),
                owner,
                token,
                generation,
                now_str,
            )
        if current is None:
            return False
        current_packet_path = str(current["task_packet_path"] or "")
        cursor = conn.execute(
            "UPDATE jobs SET status = 'awaiting_subagent', task_packet_path = ?, "
            "error_msg = ?, updated_at = ?, lease_until = NULL, lease_owner = NULL, "
            f"lease_token = NULL WHERE {predicate}",
            (
                next_packet_path,
                f"Subagent task packet: {next_packet_path}",
                now_str,
                *predicate_params,
            ),
        )
        if cursor.rowcount != 1:
            return False
        if current_packet_path and current_packet_path != next_packet_path:
            from vector_lake.native_llm import resolve_subagent_task_path

            try:
                controlled_packet = resolve_subagent_task_path(current_packet_path)
            except ValueError:
                controlled_packet = None
            if controlled_packet is not None:
                enqueue_ingest_task_cleanup(str(job_id), str(controlled_packet))
        return True


def enqueue_ingest_task_cleanup(job_id: str, task_packet_path: str) -> int:
    """Persist cleanup intent before a job releases its current task packet."""
    packet_path = str(Path(task_packet_path).resolve())
    expected_task_id = Path(packet_path).stem
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    conn.execute(
        "INSERT INTO ingest_task_cleanup ("
        "job_id, task_packet_path, expected_task_id, status, attempt_count, "
        "last_error, available_at, created_at, updated_at"
        ") VALUES (?, ?, ?, 'pending', 0, NULL, ?, ?, ?) "
        "ON CONFLICT(job_id, task_packet_path) DO UPDATE SET "
        "status = CASE WHEN ingest_task_cleanup.status IN ('completed', 'processing') "
        "THEN ingest_task_cleanup.status ELSE 'pending' END, "
        "last_error = CASE WHEN ingest_task_cleanup.status IN ('completed', 'processing') "
        "THEN ingest_task_cleanup.last_error ELSE NULL END, "
        "available_at = CASE WHEN ingest_task_cleanup.status IN ('completed', 'processing') "
        "THEN ingest_task_cleanup.available_at ELSE excluded.available_at END, "
        "updated_at = excluded.updated_at, "
        "lease_until = CASE WHEN ingest_task_cleanup.status = 'processing' "
        "THEN ingest_task_cleanup.lease_until ELSE NULL END, "
        "lease_owner = CASE WHEN ingest_task_cleanup.status = 'processing' "
        "THEN ingest_task_cleanup.lease_owner ELSE NULL END, "
        "lease_token = CASE WHEN ingest_task_cleanup.status = 'processing' "
        "THEN ingest_task_cleanup.lease_token ELSE NULL END",
        (str(job_id), packet_path, expected_task_id, now, now, now),
    )
    row = conn.execute(
        "SELECT cleanup_id FROM ingest_task_cleanup "
        "WHERE job_id = ? AND task_packet_path = ?",
        (str(job_id), packet_path),
    ).fetchone()
    return int(row["cleanup_id"])


def claim_ingest_task_cleanup(
    limit: int = 20,
    lease_seconds: int = 300,
    lease_owner: str | None = None,
) -> list[dict]:
    """Lease replayable task-packet cleanup intents."""
    import os
    import secrets
    import socket

    init_db()
    conn = get_connection()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    lease_until = (now_dt + timedelta(seconds=max(1, int(lease_seconds)))).isoformat()
    owner = str(lease_owner or f"{socket.gethostname()}:{os.getpid()}")
    claimed: list[dict] = []
    with transaction():
        rows = conn.execute(
            "SELECT cleanup_id FROM ingest_task_cleanup WHERE "
            "((status IN ('pending', 'failed') AND available_at <= ?) OR "
            "(status = 'processing' AND COALESCE(lease_until, '') <= ?)) "
            "ORDER BY cleanup_id ASC LIMIT ?",
            (now, now, max(1, int(limit))),
        ).fetchall()
        for row in rows:
            cleanup_id = int(row["cleanup_id"])
            token = secrets.token_urlsafe(32)
            cursor = conn.execute(
                "UPDATE ingest_task_cleanup SET status = 'processing', "
                "lease_until = ?, lease_owner = ?, lease_token = ?, "
                "lease_generation = lease_generation + 1, updated_at = ? "
                "WHERE cleanup_id = ? AND ("
                "(status IN ('pending', 'failed') AND available_at <= ?) OR "
                "(status = 'processing' AND COALESCE(lease_until, '') <= ?))",
                (lease_until, owner, token, now, cleanup_id, now, now),
            )
            if cursor.rowcount != 1:
                continue
            claimed_row = conn.execute(
                "SELECT * FROM ingest_task_cleanup WHERE cleanup_id = ?",
                (cleanup_id,),
            ).fetchone()
            if claimed_row is not None:
                claimed.append(dict(claimed_row))
    return claimed


def complete_ingest_task_cleanup(
    cleanup_id: int,
    lease_owner: str,
    lease_token: str,
    lease_generation: int,
) -> bool:
    """Complete one cleanup lease and clear only its still-current job pointer."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    with transaction():
        row = conn.execute(
            "SELECT job_id, task_packet_path FROM ingest_task_cleanup "
            "WHERE cleanup_id = ? AND status = 'processing' "
            "AND lease_owner = ? AND lease_token = ? AND lease_generation = ? "
            "AND lease_until > ?",
            (
                int(cleanup_id),
                str(lease_owner),
                str(lease_token),
                int(lease_generation),
                now,
            ),
        ).fetchone()
        if row is None:
            return False
        conn.execute(
            "UPDATE jobs SET task_packet_path = NULL "
            "WHERE job_id = ? AND task_packet_path = ?",
            (str(row["job_id"]), str(row["task_packet_path"])),
        )
        cursor = conn.execute(
            "UPDATE ingest_task_cleanup SET status = 'completed', "
            "completed_at = ?, updated_at = ?, last_error = NULL, "
            "lease_until = NULL, lease_owner = NULL, lease_token = NULL "
            "WHERE cleanup_id = ? AND status = 'processing' "
            "AND lease_owner = ? AND lease_token = ? AND lease_generation = ?",
            (
                now,
                now,
                int(cleanup_id),
                str(lease_owner),
                str(lease_token),
                int(lease_generation),
            ),
        )
        return cursor.rowcount == 1


def fail_ingest_task_cleanup(
    cleanup_id: int,
    lease_owner: str,
    lease_token: str,
    lease_generation: int,
    error: str,
) -> bool:
    """Release a failed cleanup lease for bounded replay."""
    conn = get_connection()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    with transaction():
        row = conn.execute(
            "SELECT attempt_count FROM ingest_task_cleanup "
            "WHERE cleanup_id = ? AND status = 'processing' "
            "AND lease_owner = ? AND lease_token = ? AND lease_generation = ? "
            "AND lease_until > ?",
            (
                int(cleanup_id),
                str(lease_owner),
                str(lease_token),
                int(lease_generation),
                now,
            ),
        ).fetchone()
        if row is None:
            return False
        attempt_count = int(row["attempt_count"] or 0) + 1
        available_at = (
            now_dt + timedelta(seconds=min(300, 2 ** min(attempt_count, 8)))
        ).isoformat()
        cursor = conn.execute(
            "UPDATE ingest_task_cleanup SET status = 'failed', "
            "attempt_count = ?, last_error = ?, available_at = ?, updated_at = ?, "
            "lease_until = NULL, lease_owner = NULL, lease_token = NULL "
            "WHERE cleanup_id = ? AND status = 'processing' "
            "AND lease_owner = ? AND lease_token = ? AND lease_generation = ?",
            (
                attempt_count,
                str(error)[:2000],
                available_at,
                now,
                int(cleanup_id),
                str(lease_owner),
                str(lease_token),
                int(lease_generation),
            ),
        )
        return cursor.rowcount == 1


def claim_subagent_jobs(
    limit: int = 10,
    lease_seconds: int = 3600,
    lease_owner: str | None = None,
    *,
    required_ingest_contract_version: int | None = None,
) -> list[dict]:
    """Lease ingest tasks to one host subagent consumer at a time."""
    import os
    import secrets
    import socket

    init_db()
    conn = get_connection()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    lease_until = (now_dt + timedelta(seconds=max(1, int(lease_seconds)))).isoformat()
    owner = str(
        lease_owner
        or os.environ.get("VECTOR_LAKE_SUBAGENT_RUN_ID")
        or f"{socket.gethostname()}:{os.getpid()}"
    )
    scope_sql = ""
    scope_params: list[object] = []
    if required_ingest_contract_version is not None:
        contract_version = int(required_ingest_contract_version)
        if contract_version < 1:
            raise ValueError("required_ingest_contract_version must be positive")
        scope_sql = f" AND ({_current_ingest_contract_sql()})"
        scope_params.append(str(contract_version))
    with transaction():
        rows = conn.execute(
            "SELECT job_id FROM jobs WHERE task_type = 'ingest' AND "
            "(status = 'awaiting_subagent' OR "
            "(status = 'subagent_processing' AND COALESCE(lease_until, '') <= ?))"
            + scope_sql
            + " ORDER BY created_at ASC LIMIT ?",
            (now, *scope_params, max(1, int(limit))),
        ).fetchall()
        job_ids = [str(row["job_id"]) for row in rows]
        if not job_ids:
            return []
        claimed = []
        for job_id in job_ids:
            lease_token = secrets.token_urlsafe(32)
            cursor = conn.execute(
                "UPDATE jobs SET status = 'subagent_processing', lease_until = ?, "
                "lease_owner = ?, lease_token = ?, "
                "lease_generation = COALESCE(lease_generation, 0) + 1, updated_at = ? "
                "WHERE job_id = ? AND (status = 'awaiting_subagent' OR "
                "(status = 'subagent_processing' AND COALESCE(lease_until, '') <= ?))"
                + scope_sql,
                (
                    lease_until,
                    owner,
                    lease_token,
                    now,
                    job_id,
                    now,
                    *scope_params,
                ),
            )
            if cursor.rowcount != 1:
                continue
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is not None:
                claimed.append(dict(row))
        return claimed


def replace_ingest_subagent_task_packet(
    job_id: str,
    task_packet_path: str,
    lease_owner: str,
    lease_token: str,
    lease_generation: int,
) -> bool:
    """Replace a bad packet pointer only while its subagent lease is current."""
    lease = _dispatch_lease_credentials(
        lease_owner,
        lease_token,
        lease_generation,
    )
    if lease is None:
        raise ValueError("subagent lease credentials are required")
    owner, token, generation = lease
    next_packet_path = str(Path(task_packet_path).resolve())
    conn = get_connection()
    with transaction():
        now = datetime.now(timezone.utc).isoformat()
        row = conn.execute(
            "SELECT task_packet_path FROM jobs WHERE job_id = ? "
            "AND task_type = 'ingest' AND status = 'subagent_processing' "
            "AND lease_owner = ? AND lease_token = ? AND lease_generation = ? "
            "AND COALESCE(lease_until, '') > ?",
            (str(job_id), owner, token, generation, now),
        ).fetchone()
        if row is None:
            return False
        current_packet_path = str(row["task_packet_path"] or "")
        cursor = conn.execute(
            "UPDATE jobs SET task_packet_path = ?, error_msg = ?, updated_at = ? "
            "WHERE job_id = ? AND task_type = 'ingest' "
            "AND status = 'subagent_processing' AND lease_owner = ? "
            "AND lease_token = ? AND lease_generation = ? "
            "AND COALESCE(lease_until, '') > ? AND task_packet_path IS ?",
            (
                next_packet_path,
                f"Subagent task packet repaired: {next_packet_path}",
                now,
                str(job_id),
                owner,
                token,
                generation,
                now,
                row["task_packet_path"],
            ),
        )
        if cursor.rowcount != 1:
            return False
        if current_packet_path and current_packet_path != next_packet_path:
            from vector_lake.native_llm import resolve_subagent_task_path

            try:
                controlled_packet = resolve_subagent_task_path(current_packet_path)
            except ValueError:
                controlled_packet = None
            if controlled_packet is not None:
                enqueue_ingest_task_cleanup(str(job_id), str(controlled_packet))
        return True


def fail_ingest_subagent_task_claim(
    job_id: str,
    lease_owner: str,
    lease_token: str,
    lease_generation: int,
    error_msg: str,
) -> bool:
    """Release an unrepaired bad-packet claim under its exact current lease."""
    lease = _dispatch_lease_credentials(
        lease_owner,
        lease_token,
        lease_generation,
    )
    if lease is None:
        raise ValueError("subagent lease credentials are required")
    owner, token, generation = lease
    conn = get_connection()
    with transaction():
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        row = conn.execute(
            "SELECT retries, task_packet_path FROM jobs WHERE job_id = ? "
            "AND task_type = 'ingest' AND status = 'subagent_processing' "
            "AND lease_owner = ? AND lease_token = ? AND lease_generation = ? "
            "AND COALESCE(lease_until, '') > ?",
            (str(job_id), owner, token, generation, now),
        ).fetchone()
        if row is None:
            return False
        next_retry = int(row["retries"] or 0) + 1
        terminal = next_retry >= 3
        available_at = (
            now_dt + timedelta(seconds=5 * (2 ** max(0, next_retry - 1)))
        ).isoformat()
        result_json = (
            json.dumps(
                {
                    "maintenance": "ingest_task_packet_repair",
                    "state": "blocked",
                    "reason": str(error_msg)[:4000],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            if terminal
            else None
        )
        cursor = conn.execute(
            "UPDATE jobs SET status = 'failed', retries = ?, error_msg = ?, "
            "updated_at = ?, available_at = ?, completed_at = ?, result_json = ?, "
            "lease_until = NULL, lease_owner = NULL, lease_token = NULL, "
            "task_packet_path = CASE WHEN ? THEN NULL ELSE task_packet_path END, "
            "idempotency_key = CASE WHEN ? THEN NULL ELSE idempotency_key END "
            "WHERE job_id = ? AND task_type = 'ingest' "
            "AND status = 'subagent_processing' AND lease_owner = ? "
            "AND lease_token = ? AND lease_generation = ? "
            "AND COALESCE(lease_until, '') > ?",
            (
                next_retry,
                str(error_msg)[:4000],
                now,
                available_at,
                now if terminal else None,
                result_json,
                int(terminal),
                int(terminal),
                str(job_id),
                owner,
                token,
                generation,
                now,
            ),
        )
        if cursor.rowcount != 1:
            return False
        packet_path = str(row["task_packet_path"] or "")
        if terminal and packet_path:
            enqueue_ingest_task_cleanup(str(job_id), packet_path)
        return True


def requeue_ingest_subagent_baseline_conflict(
    job_id: str,
    lease_owner: str,
    lease_token: str,
    lease_generation: int,
    error_msg: str,
    *,
    current_ingest_contract_version: int,
) -> bool:
    """Invalidate a stale dispatch and route it through contract rebuild."""
    lease = _dispatch_lease_credentials(
        lease_owner,
        lease_token,
        lease_generation,
    )
    if lease is None:
        raise ValueError("subagent lease credentials are required")
    contract_version = int(current_ingest_contract_version)
    if contract_version < 2:
        raise ValueError("current_ingest_contract_version must be at least 2")
    owner, token, generation = lease
    conn = get_connection()
    with transaction():
        now = datetime.now(timezone.utc).isoformat()
        row = conn.execute(
            "SELECT payload, task_packet_path FROM jobs WHERE job_id = ? "
            "AND task_type = 'ingest' AND status = 'subagent_processing' "
            "AND lease_owner = ? AND lease_token = ? AND lease_generation = ? "
            "AND COALESCE(lease_until, '') > ?",
            (str(job_id), owner, token, generation, now),
        ).fetchone()
        if row is None:
            return False
        try:
            payload = json.loads(str(row["payload"] or "{}"))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Ingest job {job_id} has an invalid payload during requeue"
            ) from exc
        if not isinstance(payload, dict):
            raise ValueError(
                f"Ingest job {job_id} payload must be an object during requeue"
            )
        payload["ingest_contract_version"] = contract_version - 1
        stale_payload = row["payload"]
        refreshed_payload = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        )
        cursor = conn.execute(
            "UPDATE jobs SET payload = ?, status = 'queued', error_msg = ?, "
            "result_json = NULL, completed_at = NULL, available_at = ?, "
            "updated_at = ?, lease_until = NULL, lease_owner = NULL, "
            "lease_token = NULL WHERE job_id = ? AND task_type = 'ingest' "
            "AND status = 'subagent_processing' AND lease_owner = ? "
            "AND lease_token = ? AND lease_generation = ? "
            "AND COALESCE(lease_until, '') > ? AND payload IS ?",
            (
                refreshed_payload,
                str(error_msg)[:4000],
                now,
                now,
                str(job_id),
                owner,
                token,
                generation,
                now,
                stale_payload,
            ),
        )
        return cursor.rowcount == 1

def validate_ingest_job_finalization(job_id: str, processed_data: dict) -> dict:
    """Bind finalization to the exact leased job payload."""
    init_db()
    row = (
        get_connection()
        .execute(
            "SELECT * FROM jobs WHERE job_id = ?",
            (str(job_id),),
        )
        .fetchone()
    )
    if row is None:
        raise ValueError(f"Unknown ingest job: {job_id}")
    if row["task_type"] != "ingest":
        raise ValueError(f"Job {job_id} is not an ingest job")
    if row["status"] != "subagent_processing":
        raise ValueError(
            f"Job {job_id} cannot be finalized from status {row['status']}"
        )
    lease_token = str(processed_data.get("lease_token") or "")
    expected_token = str(row["lease_token"] or "")
    if not lease_token or lease_token != expected_token:
        raise ValueError(f"Job {job_id} lease_token does not match the current lease")
    lease_owner = str(processed_data.get("lease_owner") or "")
    expected_owner = str(row["lease_owner"] or "")
    if not lease_owner or lease_owner != expected_owner:
        raise ValueError(f"Job {job_id} lease_owner does not match the current lease")
    try:
        lease_generation = int(processed_data.get("lease_generation"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Job {job_id} requires a valid lease_generation") from exc
    if lease_generation != int(row["lease_generation"] or 0):
        raise ValueError(
            f"Job {job_id} lease_generation does not match the current lease"
        )
    lease_until = datetime.fromisoformat(
        str(row["lease_until"] or "").replace("Z", "+00:00")
    )
    if lease_until.tzinfo is None:
        lease_until = lease_until.replace(tzinfo=timezone.utc)
    if lease_until <= datetime.now(timezone.utc):
        raise ValueError(f"Job {job_id} lease has expired")
    try:
        payload = json.loads(row["payload"] or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Job {job_id} has an invalid payload") from exc
    for key in ("filepath", "hash"):
        if str(processed_data.get(key) or "") != str(payload.get(key) or ""):
            raise ValueError(f"Job {job_id} {key} does not match its queued payload")
    expected_name = str(payload.get("canonical_name") or "")
    supplied_name = str(processed_data.get("canonical_name") or "")
    if expected_name and supplied_name != expected_name:
        raise ValueError(
            f"Job {job_id} canonical_name does not match its queued payload"
        )
    expected_source_hash = str(payload.get("source_hash") or "")
    supplied_source_hash = str(processed_data.get("source_hash") or "")
    if supplied_source_hash != expected_source_hash:
        raise ValueError(f"Job {job_id} source_hash does not match its queued payload")
    queued_projection_hash = payload.get("source_projection_hash")
    try:
        projection_binding_required = (
            int(payload.get("ingest_contract_version") or 0) >= 3
        )
    except (TypeError, ValueError):
        projection_binding_required = False
    if queued_projection_hash is None and (
        expected_source_hash or projection_binding_required
    ):
        raise ValueError(
            f"Job {job_id} is missing a source_projection_hash baseline; requeue it"
        )
    expected_projection_hash = str(queued_projection_hash or "")
    supplied_projection_hash = str(processed_data.get("source_projection_hash") or "")
    if supplied_projection_hash != expected_projection_hash:
        raise ValueError(
            f"Job {job_id} source_projection_hash does not match its queued payload"
        )
    expected_contract_version = payload.get("ingest_contract_version")
    try:
        candidate_binding_required = int(expected_contract_version or 0) >= 4
    except (TypeError, ValueError):
        candidate_binding_required = False
    if candidate_binding_required and not isinstance(
        payload.get("integration_candidates"), list
    ):
        raise ValueError(
            f"Job {job_id} is missing its integration_candidates dispatch manifest"
        )
    if expected_contract_version is not None and (
        str(processed_data.get("ingest_contract_version") or "")
        != str(expected_contract_version)
    ):
        raise ValueError(
            f"Job {job_id} ingest_contract_version does not match its queued payload"
        )
    result = dict(row)
    result["parsed_payload"] = payload
    return result


def finalize_ingest_job(
    job_id: str,
    lease_owner: str,
    lease_token: str,
    lease_generation: int,
    result_data: dict | None = None,
):
    """Mark a validated subagent job complete inside the caller's transaction."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    result_json = json.dumps(result_data or {}, ensure_ascii=False, sort_keys=True)
    cursor = conn.execute(
        "UPDATE jobs SET status = 'finalized', completed_at = ?, updated_at = ?, "
        "lease_until = NULL, lease_owner = NULL, lease_token = NULL, error_msg = '', result_json = ? "
        "WHERE job_id = ? AND status = 'subagent_processing' "
        "AND lease_owner = ? AND lease_token = ? AND lease_generation = ? AND lease_until > ?",
        (
            now,
            now,
            result_json,
            str(job_id),
            str(lease_owner),
            str(lease_token),
            int(lease_generation),
            now,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError(f"Ingest job {job_id} is no longer finalizable")


def expire_stale_subagent_jobs(max_age_seconds: int = 86400) -> int:
    """Expire stale handoffs while durably retaining cleanup for their packets."""
    init_db()
    conn = get_connection()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=max(1, int(max_age_seconds)))
    ).isoformat()
    now_str = datetime.now(timezone.utc).isoformat()
    with transaction():
        rows = conn.execute(
            "SELECT job_id, task_packet_path FROM jobs "
            "WHERE status = 'awaiting_subagent' AND updated_at < ?",
            (cutoff,),
        ).fetchall()
        for row in rows:
            packet_path = str(row["task_packet_path"] or "")
            if packet_path:
                enqueue_ingest_task_cleanup(str(row["job_id"]), packet_path)
        cursor = conn.execute(
            "UPDATE jobs SET status = 'failed', "
            "retries = COALESCE(retries, 0) + 1, "
            "error_msg = 'Subagent task packet expired before finalization', updated_at = ?, "
            "available_at = ?, lease_until = NULL, lease_owner = NULL, lease_token = NULL "
            "WHERE status = 'awaiting_subagent' AND updated_at < ?",
            (now_str, now_str, cutoff),
        )
        return int(cursor.rowcount or 0)


def update_job_status(
    job_id: str,
    status: str,
    error_msg: str = "",
    *,
    lease_owner: str | None = None,
    lease_token: str | None = None,
    lease_generation: int | None = None,
) -> bool:
    """Update a dispatched job only when the caller still owns its lease."""
    conn = get_connection()
    lease = _dispatch_lease_credentials(
        lease_owner,
        lease_token,
        lease_generation,
    )
    with transaction():
        now_dt = datetime.now(timezone.utc)
        now_str = now_dt.isoformat()
        if lease is None:
            predicate = "job_id = ? AND status != 'dispatched'"
            predicate_params: tuple = (str(job_id),)
        else:
            owner, token, generation = lease
            predicate = (
                "job_id = ? AND status = 'dispatched' AND lease_owner = ? "
                "AND lease_token = ? AND lease_generation = ? "
                "AND COALESCE(lease_until, '') > ?"
            )
            predicate_params = (
                str(job_id),
                owner,
                token,
                generation,
                now_str,
            )
        row = conn.execute(
            f"SELECT retries, status FROM jobs WHERE {predicate}",
            predicate_params,
        ).fetchone()
        if row is None:
            return False
        current_status = str(row["status"] or "")
        if lease is None:
            update_predicate = "job_id = ? AND status = ?"
            update_params = (str(job_id), current_status)
        else:
            update_predicate = predicate
            update_params = predicate_params
        if status == "failed":
            next_retry = int(row["retries"] or 0) + 1
            available_at = (
                now_dt + timedelta(seconds=5 * (2 ** max(0, next_retry - 1)))
            ).isoformat()
            cursor = conn.execute(
                "UPDATE jobs SET status = ?, error_msg = ?, updated_at = ?, "
                "retries = COALESCE(retries, 0) + 1, available_at = ?, "
                "lease_until = NULL, "
                "lease_owner = NULL, lease_token = NULL "
                f"WHERE {update_predicate}",
                (
                    status,
                    str(error_msg)[:4000],
                    now_str,
                    available_at,
                    *update_params,
                ),
            )
        else:
            completed_at = now_str if status in {"finalized", "completed"} else None
            cursor = conn.execute(
                "UPDATE jobs SET status = ?, error_msg = ?, updated_at = ?, "
                "lease_until = NULL, lease_owner = NULL, lease_token = NULL, "
                "completed_at = COALESCE(?, completed_at) "
                f"WHERE {update_predicate}",
                (
                    status,
                    str(error_msg)[:4000],
                    now_str,
                    completed_at,
                    *update_params,
                ),
            )
        return cursor.rowcount == 1


def backup_database(destination_path: str | Path | None = None):
    """Create a transactionally consistent, standalone SQLite backup."""
    import time

    if not get_db_path().exists():
        init_db()
    if destination_path is None:
        backup_dir = get_meta_dir() / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"vector_lake_{int(time.time())}.db.bak"
    else:
        backup_path = Path(destination_path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
    destination = sqlite3.connect(str(backup_path))
    try:
        get_connection().backup(destination)
        journal_mode = str(
            destination.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        ).casefold()
        if journal_mode != "delete":
            raise RuntimeError(
                "SQLite backup journal mode conversion failed: "
                f"expected delete, observed {journal_mode or '<empty>'}"
            )
        integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite backup integrity check failed: {integrity}")
    finally:
        destination.close()

    sidecars = [
        Path(f"{backup_path}{suffix}")
        for suffix in ("-wal", "-shm")
        if Path(f"{backup_path}{suffix}").exists()
    ]
    if sidecars:
        raise RuntimeError(
            "SQLite backup is not standalone; sidecar files remain: "
            + ", ".join(str(path) for path in sidecars)
        )
    return str(backup_path)
