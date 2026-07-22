"""Runtime health checks used by write gates and doctor surfaces."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_INDEX_PARITY_FIELDS = (
    "id",
    "title",
    "summary",
    "raw_text",
    "type",
    "domain",
    "topic_cluster",
    "status",
    "epistemic_status",
    "categories",
    "tags",
    "aliases",
    "relations",
    "sources",
    "tension_edges",
    "links",
    "outbound_links",
    "triples",
    "updated",
    "updated_at",
)

_CACHE_LOCK = threading.RLock()
_CANONICAL_CACHE: dict[str, Any] = {"key": None, "value": None}
_INDEX_CACHE: dict[str, Any] = {"key": None, "value": None}
_WIKI_VERSION_CACHE: dict[str, tuple[tuple[int, int, int], str | None, str | None]] = {}


def _index_projection_signature(node: dict[str, Any]) -> str:
    stable_projection = {field: node.get(field) for field in _INDEX_PARITY_FIELDS}
    return json.dumps(stable_projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_dt(value: Any):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _canonical_snapshot(conn, db_path: Path) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Load canonical entities once for an unchanged database generation."""
    generation = conn.execute(
        "SELECT COUNT(*) AS count, MAX(updated_at) AS latest, "
        "COALESCE(SUM(LENGTH(data_json)), 0) AS bytes FROM entities"
    ).fetchone()
    key = (
        str(db_path.resolve()),
        int(generation["count"] or 0),
        str(generation["latest"] or ""),
        int(generation["bytes"] or 0),
    )
    with _CACHE_LOCK:
        if _CANONICAL_CACHE.get("key") == key:
            return _CANONICAL_CACHE["value"]

    by_page: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for row in conn.execute("SELECT entity_id, data_json FROM entities ORDER BY entity_id"):
        raw = str(row["data_json"])
        try:
            entity = json.loads(raw)
        except (TypeError, ValueError):
            continue
        page_key = str(entity.get("page_key") or "")
        if not page_key or page_key.startswith("System_"):
            continue
        by_page.setdefault(page_key, []).append((str(row["entity_id"]), entity))
    with _CACHE_LOCK:
        _CANONICAL_CACHE.update({"key": key, "value": by_page})
    return by_page


def _index_snapshot(index_path: Path) -> tuple[dict[str, Any], Exception | None]:
    """Parse index.json once for an unchanged file identity."""
    try:
        stat = index_path.stat()
        key = (str(index_path.resolve()), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
    except OSError as exc:
        return {"nodes": {}}, exc
    with _CACHE_LOCK:
        if _INDEX_CACHE.get("key") == key:
            return _INDEX_CACHE["value"], None
    try:
        value = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"nodes": {}}, exc
    with _CACHE_LOCK:
        _INDEX_CACHE.update({"key": key, "value": value})
    return value, None


def _wiki_projection_version(governance_store, path: Path) -> tuple[str | None, str | None]:
    """Return a page version using a stat-keyed, process-local parse cache."""
    cache_key = str(path.resolve())
    try:
        stat = path.stat()
        identity = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
    except OSError as exc:
        return None, str(exc)
    with _CACHE_LOCK:
        cached = _WIKI_VERSION_CACHE.get(cache_key)
        if cached and cached[0] == identity:
            return cached[1], cached[2]
    try:
        version = governance_store.canonical_page_version_from_content(
            path.name,
            path.read_text(encoding="utf-8"),
        )
        error = None
    except Exception as exc:
        version = None
        error = str(exc)
    with _CACHE_LOCK:
        _WIKI_VERSION_CACHE[cache_key] = (identity, version, error)
    return version, error


def _prune_wiki_version_cache(active_paths: set[str]) -> None:
    with _CACHE_LOCK:
        stale = [key for key in _WIKI_VERSION_CACHE if key not in active_paths]
        for key in stale:
            _WIKI_VERSION_CACHE.pop(key, None)


def _clear_health_caches_for_tests() -> None:
    """Reset process-local caches; intentionally private to runtime tests."""
    with _CACHE_LOCK:
        _CANONICAL_CACHE.update({"key": None, "value": None})
        _INDEX_CACHE.update({"key": None, "value": None})
        _WIKI_VERSION_CACHE.clear()


def assess_runtime_health(
    max_watchdog_age_seconds: int = 120,
    deep_projection_checks: bool = False,
) -> dict[str, Any]:
    from vector_lake.db_store import get_connection, get_db_path, init_db
    from vector_lake.wiki_utils import get_index_path, get_meta_dir, get_wiki_dir

    issues: list[str] = []
    warnings: list[str] = []
    detail: dict[str, Any] = {}

    try:
        db_path = get_db_path()
        if not db_path.exists():
            init_db()
        conn = get_connection()
    except Exception as exc:
        return {"ok": False, "issues": [f"database_unavailable:{exc}"], "warnings": [], "detail": {}}

    detail["db_path"] = str(db_path)

    outbox_counts = {
        row["status"]: row["count"]
        for row in conn.execute("SELECT status, COUNT(*) AS count FROM mutation_outbox GROUP BY status")
    }
    detail["outbox_counts"] = outbox_counts
    if outbox_counts.get("failed", 0):
        issues.append(f"mutation_outbox_failed:{outbox_counts.get('failed', 0)}")
    oldest_pending = conn.execute(
        "SELECT MIN(COALESCE(available_at, created_at)) FROM mutation_outbox "
        "WHERE status IN ('pending', 'processing')"
    ).fetchone()[0]
    oldest_pending_dt = _parse_dt(oldest_pending)
    if oldest_pending_dt is not None:
        pending_age = max(0, int((datetime.now(timezone.utc) - oldest_pending_dt).total_seconds()))
        detail["oldest_pending_outbox_age_seconds"] = pending_age
        max_pending_age = max(1, int(os.environ.get("VECTOR_LAKE_OUTBOX_MAX_PENDING_AGE_SECONDS", "300")))
        if pending_age > max_pending_age:
            issues.append(f"mutation_outbox_stalled:{pending_age}s")

    terminal_jobs = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status = 'failed' AND retries >= 3"
    ).fetchone()[0]
    detail["terminal_failed_jobs"] = int(terminal_jobs)
    if terminal_jobs:
        issues.append(f"terminal_failed_jobs:{terminal_jobs}")
    awaiting_row = conn.execute(
        "SELECT COUNT(*) AS count, MIN(updated_at) AS oldest FROM jobs WHERE status = 'awaiting_subagent'"
    ).fetchone()
    awaiting_count = int(awaiting_row["count"] or 0)
    detail["awaiting_subagent_jobs"] = awaiting_count
    backlog_messages = []
    max_awaiting = max(1, int(os.environ.get("VECTOR_LAKE_MAX_AWAITING_SUBAGENT_JOBS", "500")))
    if awaiting_count > max_awaiting:
        backlog_messages.append(f"count={awaiting_count}")
    oldest_awaiting = _parse_dt(awaiting_row["oldest"])
    if oldest_awaiting is not None:
        awaiting_age = max(0, int((datetime.now(timezone.utc) - oldest_awaiting).total_seconds()))
        detail["oldest_awaiting_subagent_age_seconds"] = awaiting_age
        max_age = max(60, int(os.environ.get("VECTOR_LAKE_MAX_AWAITING_SUBAGENT_AGE_SECONDS", "86400")))
        if awaiting_age > max_age:
            backlog_messages.append(f"oldest={awaiting_age}s")
    if backlog_messages:
        message = "subagent_backlog:" + ",".join(backlog_messages)
        if os.environ.get("VECTOR_LAKE_SUBAGENT_BACKLOG_BLOCKING") == "1":
            issues.append(message)
        else:
            warnings.append(message)

    status_path = get_meta_dir() / ".watchdog_status.json"
    if status_path.exists():
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            updated_at = _parse_dt(status.get("updated_at"))
            age = None
            if updated_at is not None:
                age = max(0, int((datetime.now(timezone.utc) - updated_at).total_seconds()))
            detail["watchdog_age_seconds"] = age
            detail["watchdog_status"] = status.get("status")
            if age is None or age > max_watchdog_age_seconds:
                issues.append(f"watchdog_stale:{age if age is not None else 'unknown'}")
            unhealthy_components = [
                name
                for name, component in (status.get("components") or {}).items()
                if str(component.get("status", "")).lower() in {"error", "halted"}
            ]
            if str(status.get("status", "")).lower() in {"error", "halted"} or unhealthy_components:
                issues.append(
                    "watchdog_unhealthy:"
                    + (",".join(sorted(unhealthy_components)) or str(status.get("status")))
                )
        except Exception as exc:
            issues.append(f"watchdog_status_unreadable:{exc}")
    else:
        warnings.append("watchdog_status_missing")

    excluded = {"index.md", "log.md", "overview.md", "orphan_pages.md", "wiki_link_stats.md", "Synthesis_log.md"}
    wiki_dir = get_wiki_dir()
    wiki_keys = {
        path.stem for path in wiki_dir.glob("*.md")
        if path.is_file() and path.name not in excluded and not path.name.startswith("System_")
    } if wiki_dir.exists() else set()
    canonical_entities_by_page = _canonical_snapshot(conn, db_path)
    canonical_keys = set(canonical_entities_by_page)
    index_path = get_index_path()
    index_available = index_path.exists()
    index_data: dict[str, Any] = {"nodes": {}}
    if index_path.exists():
        index_data, index_error = _index_snapshot(index_path)
        if index_error is None:
            index_keys = {
                key for key in index_data.get("nodes", {})
                if not str(key).startswith("System_")
            }
        else:
            index_keys = set()
            issues.append(f"index_unreadable:{index_error}")
    else:
        index_keys = set()
        warnings.append("index_missing")

    drift = {
        "wiki": len(wiki_keys),
        "index": len(index_keys),
        "canonical": len(canonical_keys),
        "missing_index": len(canonical_keys - index_keys) if index_available else 0,
        "extra_index": len(index_keys - canonical_keys) if index_available else 0,
        "missing_canonical": len(wiki_keys - canonical_keys),
        "extra_canonical": len(canonical_keys - wiki_keys),
    }
    detail["projection_drift"] = drift
    if any(drift[key] for key in ("missing_index", "extra_index", "missing_canonical", "extra_canonical")):
        issues.append(
            "projection_drift:"
            f"missing_index={drift['missing_index']},"
            f"extra_index={drift['extra_index']},"
            f"missing_canonical={drift['missing_canonical']},"
            f"extra_canonical={drift['extra_canonical']}"
        )

    if deep_projection_checks:
        from vector_lake import governance_store
        from vector_lake.claim_extractor import extract_page_objects
        from vector_lake.indexer import _entity_to_index_node, claim_graph_projection_parity
        from vector_lake.wiki_utils import split_frontmatter

        active_projection_rows = conn.execute(
            "SELECT filename, mutation_type, payload_text, base_version "
            "FROM mutation_outbox WHERE status IN ('pending', 'processing') "
            "ORDER BY id DESC"
        ).fetchall()
        active_projection_intents = {}
        for row in active_projection_rows:
            page_key = Path(str(row["filename"])).stem
            active_projection_intents.setdefault(page_key, row)

        wiki_content_drift: list[str] = []
        unreadable_wiki: list[str] = []
        shared_wiki_keys = wiki_keys & canonical_keys
        canonical_versions = {
            page_key: governance_store._canonical_entity_records_version(
                canonical_entities_by_page[page_key]
            )
            for page_key in shared_wiki_keys
        }
        managed_base_versions = {}
        for page_key, row in active_projection_intents.items():
            if page_key not in canonical_versions:
                continue
            if str(row["mutation_type"]) != "update" or row["payload_text"] is None:
                continue
            base_version = row["base_version"]
            if base_version is None:
                continue
            try:
                payload_version = governance_store.canonical_page_version_from_content(
                    str(row["filename"]),
                    str(row["payload_text"]),
                )
            except Exception:
                continue
            if payload_version == canonical_versions.get(page_key):
                managed_base_versions[page_key] = str(base_version)
        active_wiki_paths = {
            str((wiki_dir / f"{page_key}.md").resolve())
            for page_key in shared_wiki_keys
        }
        _prune_wiki_version_cache(active_wiki_paths)
        managed_reconciliation_drift: set[str] = set()
        managed_wiki_pages: set[str] = set()
        for page_key in sorted(shared_wiki_keys):
            path = wiki_dir / f"{page_key}.md"
            observed, observed_error = _wiki_projection_version(governance_store, path)
            if observed_error is not None:
                unreadable_wiki.append(page_key)
                continue
            if observed != canonical_versions.get(page_key):
                if observed == managed_base_versions.get(page_key):
                    managed_reconciliation_drift.add(page_key)
                    managed_wiki_pages.add(page_key)
                else:
                    wiki_content_drift.append(page_key)

        index_content_drift: list[str] = []
        for page_key in sorted(index_keys & canonical_keys):
            node = index_data.get("nodes", {}).get(page_key) or {}
            expected_signatures = set()
            for entity_id, entity in canonical_entities_by_page.get(page_key, []):
                try:
                    projected_key, projected_node = _entity_to_index_node(entity, entity_id)
                except Exception:
                    continue
                if projected_key == page_key:
                    expected_signatures.add(_index_projection_signature(projected_node))
            if _index_projection_signature(node) not in expected_signatures:
                managed_index = False
                if page_key in managed_wiki_pages:
                    try:
                        path = wiki_dir / f"{page_key}.md"
                        content = path.read_text(encoding="utf-8")
                        frontmatter, body = split_frontmatter(content)
                        observed_entities = extract_page_objects(
                            path.name,
                            frontmatter,
                            body,
                        ).get("entities", [])
                        observed_signatures = {
                            _index_projection_signature(projected_node)
                            for entity in observed_entities
                            for projected_key, projected_node in [
                                _entity_to_index_node(entity, str(entity.get("entity_id") or ""))
                            ]
                            if projected_key == page_key
                        }
                        managed_index = _index_projection_signature(node) in observed_signatures
                    except Exception:
                        managed_index = False
                if managed_index:
                    managed_reconciliation_drift.add(page_key)
                else:
                    index_content_drift.append(page_key)

        content_drift = {
            "wiki_canonical": len(wiki_content_drift),
            "index_canonical": len(index_content_drift),
            "unreadable_wiki": len(unreadable_wiki),
            "wiki_samples": wiki_content_drift[:10],
            "index_samples": index_content_drift[:10],
            "unreadable_samples": unreadable_wiki[:10],
            "managed_reconciliation": len(managed_reconciliation_drift),
            "managed_reconciliation_samples": sorted(managed_reconciliation_drift)[:10],
        }
        detail["projection_content_drift"] = content_drift
        if wiki_content_drift or index_content_drift or unreadable_wiki:
            issues.append(
                "projection_content_drift:"
                f"wiki_canonical={len(wiki_content_drift)},"
                f"index_canonical={len(index_content_drift)},"
                f"unreadable_wiki={len(unreadable_wiki)}"
            )
        if managed_reconciliation_drift:
            warnings.append(f"projection_reconciliation_pending:{len(managed_reconciliation_drift)}")

        try:
            claim_graph_drift = claim_graph_projection_parity()
            detail["claim_graph_projection_drift"] = claim_graph_drift
            if any(
                claim_graph_drift[key]
                for key in ("missing_nodes", "extra_nodes", "missing_edges", "extra_edges")
            ):
                message = (
                    "claim_graph_projection_drift:"
                    f"missing_nodes={claim_graph_drift['missing_nodes']},"
                    f"extra_nodes={claim_graph_drift['extra_nodes']},"
                    f"missing_edges={claim_graph_drift['missing_edges']},"
                    f"extra_edges={claim_graph_drift['extra_edges']}"
                )
                if active_projection_rows:
                    warnings.append("claim_graph_reconciliation_pending:" + message)
                else:
                    issues.append(message)
        except Exception as exc:
            issues.append(f"claim_graph_projection_unavailable:{exc}")

    strict_timeline_parity = os.environ.get("VECTOR_LAKE_TIMELINE_PARITY_BLOCKING") == "1"
    if deep_projection_checks or strict_timeline_parity:
        from vector_lake.tool_timeline import timeline_projection_parity

        timeline_drift = timeline_projection_parity()
        detail["timeline_projection_drift"] = timeline_drift
        if timeline_drift["missing"] or timeline_drift["extra"]:
            message = (
                "timeline_projection_drift:"
                f"missing={timeline_drift['missing']},extra={timeline_drift['extra']}"
            )
            if strict_timeline_parity:
                issues.append(message)
            else:
                warnings.append(message)

    return {"ok": not issues, "issues": issues, "warnings": warnings, "detail": detail}


def _evidence_foundation_metrics(conn) -> dict[str, Any]:
    """Summarize strict evidence-chain coverage without promoting legacy claims."""
    def count(sql: str) -> int:
        row = conn.execute(sql).fetchone()
        return int(row[0] or 0)

    claim_total = count("SELECT COUNT(*) FROM claims")
    evidence_total = count("SELECT COUNT(*) FROM evidence")
    source_total = count("SELECT COUNT(*) FROM sources")
    metrics = {
        "claim_total": claim_total,
        "claim_with_evidence_refs": count(
            "SELECT COUNT(*) FROM claims WHERE "
            "COALESCE(json_array_length(json_extract(data_json, '$.evidence_ids')), 0) > 0"
        ),
        "claim_with_extraction_run": count(
            "SELECT COUNT(*) FROM claims WHERE "
            "NULLIF(json_extract(data_json, '$.extraction_run_id'), '') IS NOT NULL"
        ),
        "claim_with_supported_assessment": count(
            "SELECT COUNT(DISTINCT c.claim_id) FROM claims AS c "
            "JOIN claim_assessments AS a ON a.claim_id = c.claim_id "
            "WHERE a.outcome = 'supported'"
        ),
        "evidence_total": evidence_total,
        "evidence_with_raw_locator": count(
            "SELECT COUNT(*) FROM evidence WHERE "
            "json_type(data_json, '$.source_locator') = 'object' AND "
            "COALESCE(json_extract(data_json, '$.source_locator.kind'), 'unresolved') != 'unresolved'"
        ),
        "evidence_lineage_safe": count(
            "SELECT COUNT(*) FROM evidence WHERE "
            "json_extract(data_json, '$.lineage_safe') = 1"
        ),
        "source_total": source_total,
        "source_integrity_verified": count(
            "SELECT COUNT(*) FROM sources WHERE "
            "json_extract(data_json, '$.integrity_status') = 'verified' AND "
            "length(json_extract(data_json, '$.content_hash')) = 64"
        ),
        "source_artifact_total": count("SELECT COUNT(*) FROM source_artifacts"),
        "source_artifact_verified": count(
            "SELECT COUNT(*) FROM source_artifacts WHERE "
            "integrity_status = 'verified' AND length(sha256) = 64"
        ),
        "extraction_run_total": count("SELECT COUNT(*) FROM extraction_runs"),
    }

    def ratio(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 1.0

    metrics.update(
        {
            "claim_evidence_coverage": ratio(metrics["claim_with_evidence_refs"], claim_total),
            "claim_extraction_coverage": ratio(metrics["claim_with_extraction_run"], claim_total),
            "claim_assessment_coverage": ratio(
                metrics["claim_with_supported_assessment"], claim_total
            ),
            "evidence_raw_locator_coverage": ratio(
                metrics["evidence_with_raw_locator"], evidence_total
            ),
            "evidence_lineage_coverage": ratio(metrics["evidence_lineage_safe"], evidence_total),
            "source_integrity_coverage": ratio(
                metrics["source_integrity_verified"], source_total
            ),
        }
    )
    return metrics


def _runtime_unsupported_governance(conn) -> dict[str, int]:
    """Classify unsupported runtime memories by active claim governance."""
    from vector_lake.governance_metrics import claim_governance_version

    unsupported_rows = conn.execute(
        "SELECT memory_id, NULLIF(json_extract(data_json, '$.source_claim_id'), '') "
        "AS source_claim_id FROM operational_memory "
        "WHERE json_extract(data_json, '$.validity_state') = 'unsupported'"
    ).fetchall()
    if not unsupported_rows:
        return {"total": 0, "managed": 0, "unmanaged": 0}

    claims_by_id = {
        str(row["claim_id"]): json.loads(row["data_json"])
        for row in conn.execute(
            "SELECT c.claim_id, c.data_json FROM claims AS c JOIN ("
            "SELECT DISTINCT json_extract(data_json, '$.source_claim_id') AS claim_id "
            "FROM operational_memory "
            "WHERE json_extract(data_json, '$.validity_state') = 'unsupported'"
            ") AS runtime_claims ON runtime_claims.claim_id = c.claim_id"
        )
    }
    governance_by_claim: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(
        "SELECT data_json FROM governance_queue "
        "WHERE json_extract(data_json, '$.type') = 'evidence-gap' "
        "AND json_extract(data_json, '$.status') = 'acknowledged' "
        "AND json_extract(data_json, '$.claim_id') IS NOT NULL"
    ):
        item = json.loads(row["data_json"])
        claim_id = str(item.get("claim_id") or "")
        governance_by_claim.setdefault(claim_id, []).append(item)

    now = datetime.now(timezone.utc)
    managed = 0
    for row in unsupported_rows:
        claim_id = str(row["source_claim_id"] or "")
        claim = claims_by_id.get(claim_id)
        if claim is None:
            continue
        claim_version = claim_governance_version(claim)
        if any(
            str(item.get("owner") or "").strip()
            and (due_at := _parse_dt(item.get("due_at"))) is not None
            and due_at >= now
            and str(item.get("claim_version") or "") == claim_version
            for item in governance_by_claim.get(claim_id, [])
        ):
            managed += 1

    total = len(unsupported_rows)
    return {"total": total, "managed": managed, "unmanaged": total - managed}


def _assess_decision_scope(
    conn,
    decision_id: str,
    index_data: dict[str, Any],
    initial_issues: list[str],
) -> dict[str, Any]:
    """Evaluate only evidence and governance debt mapped to one verified decision."""
    from vector_lake.decision_registry import get_critical_decision

    issues = list(initial_issues)
    warnings: list[str] = []
    detail: dict[str, Any] = {"scope": "critical_decision", "decision_id": decision_id}
    decision = get_critical_decision(decision_id)
    if not decision or not decision.get("registry_verified") or decision.get("status") != "active":
        issues.append(f"critical_decision_unverified:{decision_id}")
        detail["decision"] = decision
    else:
        detail["decision"] = decision

    graph_state = dict((index_data or {}).get("graph_state") or {})
    detail["graph_state"] = graph_state
    if graph_state.get("dirty") is True:
        warnings.append(
            f"global_graph_debt_outside_decision_scope:{graph_state.get('reason') or 'unknown'}"
        )
    elif not graph_state:
        warnings.append("graph_state_missing")

    scoped_pending = []
    for row in conn.execute(
        "SELECT item_id, data_json FROM governance_queue "
        "WHERE json_extract(data_json, '$.status') = 'pending'"
    ).fetchall():
        item = json.loads(row["data_json"])
        if decision_id in list(item.get("critical_decision_refs") or []):
            scoped_pending.append(str(row["item_id"]))
    detail["scoped_pending_governance_ids"] = scoped_pending
    if scoped_pending:
        issues.append(f"decision_governance_pending:{len(scoped_pending)}")

    claim_refs = list((decision or {}).get("claim_refs") or [])
    detail["claim_refs"] = claim_refs
    claim_checks = []
    if decision and not claim_refs:
        issues.append("decision_claim_scope_missing")
    for claim_id in claim_refs:
        check = {
            "claim_id": claim_id,
            "claim_exists": False,
            "evidence_complete": False,
            "source_integrity_complete": False,
            "raw_locator_complete": False,
            "lineage_safe": False,
            "assessment_supported": False,
        }
        row = conn.execute(
            "SELECT data_json FROM claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()
        if row is None:
            issues.append(f"decision_claim_missing:{claim_id}")
            claim_checks.append(check)
            continue
        check["claim_exists"] = True
        claim = json.loads(row["data_json"])
        evidence_ids = list(dict.fromkeys(claim.get("evidence_ids") or []))
        source_ids = list(dict.fromkeys(claim.get("source_ids") or []))
        evidence_records = []
        for evidence_id in evidence_ids:
            evidence_row = conn.execute(
                "SELECT data_json FROM evidence WHERE evidence_id = ?", (evidence_id,)
            ).fetchone()
            if evidence_row is not None:
                evidence = json.loads(evidence_row["data_json"])
                evidence_records.append(evidence)
                source_id = str(evidence.get("source_id") or "")
                if source_id and source_id not in source_ids:
                    source_ids.append(source_id)
        check["evidence_complete"] = bool(evidence_ids) and len(evidence_records) == len(evidence_ids)
        check["raw_locator_complete"] = bool(evidence_records) and all(
            isinstance(record.get("source_locator"), dict)
            and record["source_locator"].get("kind") != "unresolved"
            for record in evidence_records
        )
        check["lineage_safe"] = bool(evidence_records) and all(
            record.get("lineage_safe") is True for record in evidence_records
        )
        source_records = []
        for source_id in source_ids:
            source_row = conn.execute(
                "SELECT data_json FROM sources WHERE source_id = ?", (source_id,)
            ).fetchone()
            if source_row is not None:
                source_records.append(json.loads(source_row["data_json"]))
        check["source_integrity_complete"] = bool(source_ids) and len(source_records) == len(source_ids) and all(
            record.get("integrity_status") == "verified"
            and isinstance(record.get("content_hash"), str)
            and len(record["content_hash"]) == 64
            for record in source_records
        )
        check["assessment_supported"] = conn.execute(
            "SELECT 1 FROM claim_assessments WHERE claim_id = ? AND outcome = 'supported' LIMIT 1",
            (claim_id,),
        ).fetchone() is not None
        for field in (
            "evidence_complete",
            "source_integrity_complete",
            "raw_locator_complete",
            "lineage_safe",
            "assessment_supported",
        ):
            if not check[field]:
                issues.append(f"decision_claim_{field}_failed:{claim_id}")
        claim_checks.append(check)
    detail["claim_checks"] = claim_checks
    detail["global_debt_policy"] = "reported_only_when_not_mapped_to_decision"
    status = "not_ready" if issues else ("degraded" if warnings else "ready")
    return {
        "ready": status == "ready",
        "status": status,
        "issues": issues,
        "warnings": warnings,
        "detail": detail,
    }


def assess_semantic_readiness(
    index_data: dict[str, Any] | None = None,
    decision_id: str | None = None,
) -> dict[str, Any]:
    """Assess whether governed knowledge is ready for decision-support use.

    This surface is deliberately separate from runtime health. Semantic debt
    never blocks canonical repair writes, while infrastructure health never
    implies that claims or topology are ready for business decisions. An
    unsupported runtime claim blocks only while it lacks active, version-bound
    governance ownership.
    """
    from vector_lake.db_store import get_connection, get_db_path, init_db
    from vector_lake.wiki_utils import get_index_path

    issues: list[str] = []
    warnings: list[str] = []
    detail: dict[str, Any] = {}
    try:
        if not get_db_path().exists():
            init_db()
        conn = get_connection()
    except Exception as exc:
        return {
            "ready": False,
            "status": "not_ready",
            "issues": [f"database_unavailable:{exc}"],
            "warnings": [],
            "detail": {},
        }

    if index_data is None:
        index_path = get_index_path()
        if index_path.exists():
            index_data, index_error = _index_snapshot(index_path)
            if index_error is not None:
                issues.append(f"index_unreadable:{index_error}")
        else:
            index_data = {}
            issues.append("index_missing")

    normalized_decision_id = str(decision_id or "").strip()
    if normalized_decision_id:
        return _assess_decision_scope(
            conn,
            normalized_decision_id,
            dict(index_data or {}),
            issues,
        )

    graph_state = dict((index_data or {}).get("graph_state") or {})
    detail["graph_state"] = graph_state
    if graph_state.get("dirty") is True:
        issues.append(f"graph_topology_dirty:{graph_state.get('reason') or 'unknown'}")
    elif not graph_state:
        warnings.append("graph_state_missing")
    detail["graph_insight_count"] = len((index_data or {}).get("graph_insights") or [])

    pending_rows = conn.execute(
        "SELECT json_extract(data_json, '$.type') AS item_type, COUNT(*) AS count "
        "FROM governance_queue WHERE json_extract(data_json, '$.status') = 'pending' "
        "GROUP BY item_type"
    ).fetchall()
    pending_by_type = {
        str(row["item_type"] or "unknown"): int(row["count"] or 0)
        for row in pending_rows
    }
    pending_total = sum(pending_by_type.values())
    critical_types = {"contradiction", "evidence-gap", "publish-candidate"}
    critical_pending = sum(pending_by_type.get(item_type, 0) for item_type in critical_types)
    detail["pending_governance_by_type"] = pending_by_type
    detail["pending_governance_total"] = pending_total
    detail["critical_pending_governance"] = critical_pending
    max_pending = max(
        0,
        int(os.environ.get("VECTOR_LAKE_MAX_PENDING_GOVERNANCE_ITEMS", "500")),
    )
    if critical_pending:
        issues.append(f"critical_governance_pending:{critical_pending}")
    if pending_total > max_pending:
        issues.append(f"governance_backlog:{pending_total}>{max_pending}")
    elif pending_total:
        warnings.append(f"governance_pending:{pending_total}")

    validity_counts = {
        str(row["validity_state"] or "unknown"): int(row["count"] or 0)
        for row in conn.execute(
            "SELECT json_extract(data_json, '$.validity_state') AS validity_state, "
            "COUNT(*) AS count FROM operational_memory GROUP BY validity_state"
        )
    }
    detail["runtime_validity_state_counts"] = validity_counts
    unsupported = validity_counts.get("unsupported", 0)
    if unsupported:
        unsupported_governance = _runtime_unsupported_governance(conn)
        detail["runtime_unsupported_governance"] = unsupported_governance
        max_unmanaged = max(
            0,
            int(
                os.environ.get(
                    "VECTOR_LAKE_MAX_UNMANAGED_UNSUPPORTED_RUNTIME_CLAIMS",
                    os.environ.get("VECTOR_LAKE_MAX_UNSUPPORTED_RUNTIME_CLAIMS", "0"),
                )
            ),
        )
        unmanaged = unsupported_governance["unmanaged"]
        if unmanaged > max_unmanaged:
            issues.append(
                f"unmanaged_unsupported_runtime_claims:{unmanaged}>{max_unmanaged}"
            )
        managed = unsupported_governance["managed"]
        if managed:
            warnings.append(f"managed_unsupported_runtime_claims:{managed}")
    if validity_counts.get("provisional", 0):
        warnings.append(f"provisional_runtime_claims:{validity_counts['provisional']}")
    if validity_counts.get("expired", 0):
        warnings.append(f"expired_runtime_claims:{validity_counts['expired']}")

    foundation = _evidence_foundation_metrics(conn)
    detail["evidence_foundation"] = foundation
    coverage_thresholds = {
        "claim_evidence_coverage": "VECTOR_LAKE_MIN_CLAIM_EVIDENCE_COVERAGE",
        "claim_extraction_coverage": "VECTOR_LAKE_MIN_CLAIM_EXTRACTION_COVERAGE",
        "claim_assessment_coverage": "VECTOR_LAKE_MIN_CLAIM_ASSESSMENT_COVERAGE",
        "evidence_raw_locator_coverage": "VECTOR_LAKE_MIN_EVIDENCE_LOCATOR_COVERAGE",
        "evidence_lineage_coverage": "VECTOR_LAKE_MIN_EVIDENCE_LINEAGE_COVERAGE",
        "source_integrity_coverage": "VECTOR_LAKE_MIN_SOURCE_INTEGRITY_COVERAGE",
    }
    denominator_by_metric = {
        "claim_evidence_coverage": foundation["claim_total"],
        "claim_extraction_coverage": foundation["claim_total"],
        "claim_assessment_coverage": foundation["claim_total"],
        "evidence_raw_locator_coverage": foundation["evidence_total"],
        "evidence_lineage_coverage": foundation["evidence_total"],
        "source_integrity_coverage": foundation["source_total"],
    }
    for metric, env_name in coverage_thresholds.items():
        threshold = min(1.0, max(0.0, float(os.environ.get(env_name, "0.95"))))
        coverage = float(foundation[metric])
        if denominator_by_metric[metric] and coverage < threshold:
            warnings.append(f"{metric}_low:{coverage:.4f}<{threshold:.4f}")

    awaiting_row = conn.execute(
        "SELECT COUNT(*) AS count, MIN(updated_at) AS oldest "
        "FROM jobs WHERE status = 'awaiting_subagent'"
    ).fetchone()
    awaiting_count = int(awaiting_row["count"] or 0)
    detail["awaiting_subagent_jobs"] = awaiting_count
    oldest_awaiting = _parse_dt(awaiting_row["oldest"])
    if oldest_awaiting is not None:
        awaiting_age = max(
            0,
            int((datetime.now(timezone.utc) - oldest_awaiting).total_seconds()),
        )
        detail["oldest_awaiting_subagent_age_seconds"] = awaiting_age
        max_age = max(
            60,
            int(os.environ.get("VECTOR_LAKE_MAX_AWAITING_SUBAGENT_AGE_SECONDS", "86400")),
        )
        if awaiting_age > max_age:
            issues.append(f"semantic_ingest_backlog:oldest={awaiting_age}s>{max_age}s")

    status = "not_ready" if issues else ("degraded" if warnings else "ready")
    return {
        "ready": status == "ready",
        "status": status,
        "issues": issues,
        "warnings": warnings,
        "detail": detail,
    }


def enforce_runtime_write_health(validation_mode: str = "full"):
    if os.environ.get("VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE") == "1":
        return
    if validation_mode == "schema":
        return
    health = assess_runtime_health(deep_projection_checks=True)
    if not health["ok"]:
        raise RuntimeError(
            "Vector Lake write gate blocked this mutation because runtime health is not clean. "
            "Use validation_mode='schema' for bounded repairs or set VECTOR_LAKE_DISABLE_WRITE_HEALTH_GATE=1 after explicit approval. "
            f"Issues: {'; '.join(health['issues'])}"
        )
