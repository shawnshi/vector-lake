import copy
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from vector_lake import db_store, governance_store
from vector_lake.merge_analysis import preflight_suggestion
from vector_lake.mutation_coordinator import execute_mutation_batch
from vector_lake.semantic_merge import merge_markdown_content
from vector_lake.wiki_utils import (
    VALID_PREFIXES,
    get_memory_dir,
    get_wiki_dir,
    iter_markdown_files,
    iter_wiki_link_matches,
    normalize_semantic_text,
    semantic_text_hash,
    split_frontmatter,
)


log = logging.getLogger("governance_service")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_md_file(wiki_dir: Path, page_key: str, name: str) -> Path | None:
    safe_page_key = (
        page_key
        if isinstance(page_key, str)
        and page_key
        and "/" not in page_key
        and "\\" not in page_key
        else ""
    )
    safe_name = (
        name
        if isinstance(name, str) and name and "/" not in name and "\\" not in name
        else ""
    )
    candidate_names = [
        f"{safe_page_key}.md" if safe_page_key else "",
        *(f"{prefix}{safe_name}.md" for prefix in VALID_PREFIXES if safe_name),
        f"{safe_name}.md" if safe_name else "",
    ]
    casefold_matches: dict[str, list[Path]] = {}
    for path in iter_markdown_files(wiki_dir):
        casefold_matches.setdefault(path.name.casefold(), []).append(path.resolve())
    for candidate_name in candidate_names:
        if not candidate_name:
            continue
        path = (wiki_dir / candidate_name).resolve()
        if path.parent == wiki_dir and path.exists():
            return path
        matches = casefold_matches.get(candidate_name.casefold(), [])
        if len(matches) == 1 and matches[0].parent == wiki_dir:
            return matches[0]
    return None


def _validate_alias_redirect(source_id: str, target_id: str) -> None:
    visited = set()
    current = target_id
    while current:
        if current == source_id:
            raise RuntimeError("Alias redirect would create a cycle.")
        if current in visited:
            raise RuntimeError("Existing alias registry contains a cycle.")
        visited.add(current)
        current = governance_store.get_alias(current)


def _source_rows_snapshot(page_names: set[str], raw_identity: str) -> list[dict]:
    rows = (
        db_store.get_connection()
        .execute(
            "SELECT source_id, data_json, updated_at FROM sources ORDER BY source_id"
        )
        .fetchall()
    )
    snapshots = []
    for row in rows:
        data = json.loads(row["data_json"] or "{}")
        if str(data.get("canonical_source_page") or "") in page_names or (
            raw_identity and str(data.get("raw_ref") or "") == raw_identity
        ):
            snapshots.append(
                {
                    "source_id": row["source_id"],
                    "data": data,
                    "updated_at": row["updated_at"],
                }
            )
    return snapshots


def _source_artifacts_snapshot(source_rows: list[dict]) -> list[dict]:
    source_ids = sorted(
        {
            str(row.get("source_id") or "")
            for row in source_rows
            if str(row.get("source_id") or "")
        }
    )
    if not source_ids:
        return []
    placeholders = ",".join("?" for _ in source_ids)
    rows = (
        db_store.get_connection()
        .execute(
            "SELECT artifact_id, source_id, sha256, byte_size, mime_type, storage_uri, "
            "integrity_status, data_json, recorded_at FROM source_artifacts "
            f"WHERE source_id IN ({placeholders}) ORDER BY artifact_id",
            source_ids,
        )
        .fetchall()
    )
    return [
        {
            "artifact_id": row["artifact_id"],
            "source_id": row["source_id"],
            "sha256": row["sha256"],
            "byte_size": row["byte_size"],
            "mime_type": row["mime_type"],
            "storage_uri": row["storage_uri"],
            "integrity_status": row["integrity_status"],
            "data": json.loads(row["data_json"] or "{}"),
            "recorded_at": row["recorded_at"],
        }
        for row in rows
    ]


def restore_preserved_source_rows_compare_and_swap(
    expected_rows: list[dict],
    observed_json_by_id: dict[str, str],
    *,
    updated_at: str | None = None,
) -> None:
    """Restore journaled Source rows only if their exact observed JSON is current.

    The caller must own the surrounding transaction so a conflict also rolls
    back journal and governance-state changes.
    """
    conn = db_store.get_connection()
    now = updated_at or _utc_now()
    for expected in expected_rows:
        source_id = str(expected.get("source_id") or "")
        observed_json = observed_json_by_id.get(source_id)
        if not source_id or observed_json is None:
            raise RuntimeError("Source metadata recovery is missing its observed row.")
        current = conn.execute(
            "SELECT data_json FROM sources WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        if current is None or current["data_json"] != observed_json:
            raise RuntimeError(
                f"Source row changed before metadata recovery: {source_id}"
            )
        result = conn.execute(
            "UPDATE sources SET data_json = ?, updated_at = ? "
            "WHERE source_id = ? AND data_json = ?",
            (
                json.dumps(expected.get("data") or {}, ensure_ascii=False),
                now,
                source_id,
                observed_json,
            ),
        )
        if result.rowcount != 1:
            raise RuntimeError(
                f"Source row compare-and-swap failed during metadata recovery: {source_id}"
            )


def _merge_durable_source_record(current: dict, prior: dict, context: str) -> dict:
    """Preserve reviewed fields while rejecting conflicting verified identity."""
    merged = copy.deepcopy(current)
    current_verified = str(current.get("integrity_status") or "") == "verified"
    prior_verified = str(prior.get("integrity_status") or "") == "verified"
    for field, prior_value in prior.items():
        if field == "canonical_source_page":
            continue
        current_value = merged.get(field)
        if field in {"source_id", "raw_ref"}:
            if current_value not in {None, "", prior_value}:
                raise RuntimeError(f"Conflicting {field} while preserving {context}.")
            merged[field] = copy.deepcopy(prior_value)
            continue
        if field in {"content_hash", "sha256", "byte_size"}:
            if current_value in {None, ""}:
                merged[field] = copy.deepcopy(prior_value)
                continue
            if prior_value in {None, ""} or current_value == prior_value:
                continue
            if field == "content_hash" and not prior_verified:
                merged.setdefault("legacy_content_hash", copy.deepcopy(prior_value))
                continue
            raise RuntimeError(
                f"Conflicting verified {field} while preserving {context}."
            )
        if field == "artifact_id" and current_value not in {None, "", prior_value}:
            if prior_verified or current_verified:
                raise RuntimeError(
                    f"Conflicting verified artifact_id while preserving {context}."
                )
            merged.setdefault("legacy_artifact_id", copy.deepcopy(prior_value))
            continue
        if field == "integrity_status":
            if prior_verified and not current_verified:
                raise RuntimeError(
                    f"Verified integrity would be downgraded while preserving {context}."
                )
            if current_verified:
                continue
        merged[field] = copy.deepcopy(prior_value)
    return merged


def _preserve_source_metadata(
    source_rows: list[dict],
    source_artifacts: list[dict],
    raw_identity: str,
    target_filename: str,
) -> tuple[list[dict], list[dict]]:
    """Merge pre-merge Source metadata back into the survivor in the caller transaction."""
    relevant_rows = [
        row
        for row in source_rows
        if str((row.get("data") or {}).get("raw_ref") or "") == raw_identity
    ]
    if not relevant_rows:
        return [], []
    conn = db_store.get_connection()
    for prior_row in relevant_rows:
        source_id = str(prior_row.get("source_id") or "")
        current_row = conn.execute(
            "SELECT data_json FROM sources WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        merged_source = (
            json.loads(current_row["data_json"] or "{}")
            if current_row is not None
            else copy.deepcopy(prior_row.get("data") or {})
        )
        merged_source = _merge_durable_source_record(
            merged_source,
            prior_row.get("data") or {},
            f"Source row {source_id}",
        )
        merged_source["canonical_source_page"] = target_filename
        conn.execute(
            "INSERT INTO sources (source_id, data_json, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(source_id) DO UPDATE SET data_json = excluded.data_json, "
            "updated_at = excluded.updated_at",
            (
                source_id,
                json.dumps(merged_source, ensure_ascii=False),
                _utc_now(),
            ),
        )

    relevant_source_ids = {str(row["source_id"]) for row in relevant_rows}
    for prior_artifact in source_artifacts:
        if str(prior_artifact.get("source_id") or "") not in relevant_source_ids:
            continue
        artifact_id = str(prior_artifact.get("artifact_id") or "")
        current_artifact = conn.execute(
            "SELECT artifact_id, source_id, sha256, byte_size, mime_type, storage_uri, "
            "integrity_status, data_json, recorded_at FROM source_artifacts "
            "WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()
        if current_artifact is None:
            raise RuntimeError(
                f"Source artifact disappeared while merging {artifact_id}."
            )
        current_data = json.loads(current_artifact["data_json"] or "{}")
        merged_data = _merge_durable_source_record(
            current_data,
            prior_artifact.get("data") or {},
            f"Source artifact {artifact_id}",
        )
        conn.execute(
            "UPDATE source_artifacts SET sha256 = ?, byte_size = ?, mime_type = ?, "
            "storage_uri = ?, integrity_status = ?, data_json = ? WHERE artifact_id = ?",
            (
                merged_data.get("sha256") or merged_data.get("content_hash"),
                merged_data.get("byte_size"),
                merged_data.get("mime_type"),
                merged_data.get("storage_uri"),
                merged_data.get("integrity_status")
                or current_artifact["integrity_status"],
                json.dumps(merged_data, ensure_ascii=False, sort_keys=True),
                artifact_id,
            ),
        )
    preserved_rows = _source_rows_snapshot({target_filename}, raw_identity)
    preserved_artifacts = _source_artifacts_snapshot(preserved_rows)
    return preserved_rows, preserved_artifacts


def _direct_backlinks(source_key: str, excluded: set[str]) -> list[str]:
    backlinks = []
    excluded_identities = {name.casefold() for name in excluded}
    for path in iter_markdown_files(get_wiki_dir()):
        if path.name.casefold() in excluded_identities:
            continue
        content = path.read_text(encoding="utf-8")
        targets = set()
        for match in iter_wiki_link_matches(content):
            target = match.group(1).strip()
            targets.add(target[:-3] if target.casefold().endswith(".md") else target)
        if source_key in targets:
            backlinks.append(path.name)
    return sorted(backlinks)


def _current_raw_artifact_hash(raw_identity: str) -> str:
    memory_root = get_memory_dir().resolve()
    raw_root = (memory_root / "raw").resolve()
    raw_path = (memory_root / Path(raw_identity)).resolve()
    if not raw_path.is_relative_to(raw_root) or not raw_path.is_file():
        raise RuntimeError(
            f"Source raw identity is missing or outside raw root: {raw_identity}"
        )
    return hashlib.sha256(raw_path.read_bytes()).hexdigest()


def _verified_survivor_source_title(
    content: str,
    expected_projection_hash: str,
) -> str | None:
    """Return the Source title only from the hash-verified content buffer."""
    if semantic_text_hash(content) != expected_projection_hash:
        return None
    try:
        frontmatter, _body = split_frontmatter(content)
    except Exception:
        return None
    if not isinstance(frontmatter, dict) or frontmatter.get("type") != "source":
        return None
    title = frontmatter.get("title")
    return title if isinstance(title, str) and title else None


def _legacy_storage_uri_normalized_to_canonical(
    expected_uri: object,
    observed_uri: object,
    raw_ref: object,
) -> bool:
    """Accept only the historical .gemini/MEMORY -> canonical MEMORY URI move."""
    if not all(isinstance(value, str) and value for value in (expected_uri, observed_uri, raw_ref)):
        return False
    normalized_raw_ref = str(raw_ref).replace("\\", "/")
    if not normalized_raw_ref.startswith("raw/"):
        return False
    memory_root = get_memory_dir().resolve()
    raw_root = (memory_root / "raw").resolve()
    canonical_artifact = (memory_root / Path(normalized_raw_ref)).resolve()
    if not canonical_artifact.is_relative_to(raw_root) or canonical_artifact == raw_root:
        return False
    legacy_memory_root = (memory_root.parent / ".gemini" / memory_root.name).resolve()
    legacy_artifact = (legacy_memory_root / Path(normalized_raw_ref)).resolve()
    return (
        str(expected_uri).casefold() == legacy_artifact.as_uri().casefold()
        and str(observed_uri).casefold() == canonical_artifact.as_uri().casefold()
    )


def _preserved_source_data_matches(
    expected: dict,
    observed: dict,
    *,
    canonical_source_title: str | None = None,
) -> bool:
    """Allow only hash-bound extractor normalization of the derived title.

    A raw-derived Source row can initially use the raw basename as its title,
    including ``.md``.  Re-projecting the canonical Source page refreshes that
    derived field from hash-verified frontmatter.  Every other field remains
    part of the exact preservation contract.
    """
    if observed == expected:
        return True
    if not isinstance(expected, dict) or not isinstance(observed, dict):
        return False
    expected_title = expected.get("title")
    observed_title = observed.get("title")
    if expected_title == observed_title and isinstance(expected_title, str):
        title_matches = True
    elif (
        isinstance(expected_title, str)
        and isinstance(observed_title, str)
        and expected_title.endswith(".md")
        and expected_title[:-3] == observed_title
    ):
        title_matches = True
    else:
        title_matches = (
            isinstance(observed_title, str)
            and isinstance(canonical_source_title, str)
            and observed_title == canonical_source_title
        )
    if not title_matches:
        return False
    expected_rest = {key: value for key, value in expected.items() if key != "title"}
    observed_rest = {key: value for key, value in observed.items() if key != "title"}
    if observed_rest == expected_rest:
        return True
    expected_storage_uri = expected_rest.pop("storage_uri", None)
    observed_storage_uri = observed_rest.pop("storage_uri", None)
    return observed_rest == expected_rest and _legacy_storage_uri_normalized_to_canonical(
        expected_storage_uri,
        observed_storage_uri,
        expected_rest.get("raw_ref"),
    )


def _post_merge_errors(candidate: dict, journal: dict) -> list[str]:
    wiki_dir = get_wiki_dir()
    target_name = str(
        journal.get("target_filename") or f"{candidate['left_page_key']}.md"
    )
    source_name = str(
        journal.get("source_filename") or f"{candidate['right_page_key']}.md"
    )
    target_path = wiki_dir / target_name
    source_path = wiki_dir / source_name
    errors = []
    canonical_source_title = None
    if not target_path.is_file():
        errors.append(f"missing survivor projection: {target_name}")
    else:
        target_content = target_path.read_text(encoding="utf-8")
        canonical_source_title = _verified_survivor_source_title(
            target_content,
            str(journal.get("merged_projection_hash") or ""),
        )
        if semantic_text_hash(target_content) != journal.get("merged_projection_hash"):
            errors.append(f"survivor projection hash mismatch: {target_name}")
    if source_path.exists():
        errors.append(f"duplicate projection still exists: {source_name}")
    if (
        governance_store.get_alias(candidate["right_entity_id"])
        != candidate["left_entity_id"]
    ):
        errors.append("merge alias redirect is missing")
    backlinks = _direct_backlinks(
        candidate["right_page_key"],
        {target_name, source_name},
    )
    if backlinks:
        errors.append("duplicate backlinks remain: " + ", ".join(backlinks[:10]))

    raw_identity = str(candidate.get("source_identity") or "")
    if raw_identity:
        try:
            current_artifact_hash = _current_raw_artifact_hash(raw_identity)
        except RuntimeError as exc:
            errors.append(str(exc))
        else:
            if current_artifact_hash != candidate.get("source_artifact_hash"):
                errors.append(
                    f"Source raw artifact changed after merge: {raw_identity}"
                )
        rows = (
            db_store.get_connection()
            .execute(
                "SELECT data_json FROM sources WHERE json_extract(data_json, '$.raw_ref') = ?",
                (raw_identity,),
            )
            .fetchall()
        )
        if not rows:
            errors.append(f"canonical source row is missing: {raw_identity}")
        else:
            wrong_pages = {
                str(
                    json.loads(row["data_json"] or "{}").get("canonical_source_page")
                    or ""
                )
                for row in rows
                if str(
                    json.loads(row["data_json"] or "{}").get("canonical_source_page")
                    or ""
                )
                != target_name
            }
            if wrong_pages:
                errors.append(
                    "canonical source row points outside survivor: "
                    + ", ".join(sorted(wrong_pages))
                )
    for expected in journal.get("preserved_source_rows") or []:
        row = (
            db_store.get_connection()
            .execute(
                "SELECT data_json FROM sources WHERE source_id = ?",
                (str(expected.get("source_id") or ""),),
            )
            .fetchone()
        )
        observed = json.loads(row["data_json"] or "{}") if row is not None else None
        if row is None or not _preserved_source_data_matches(
            expected.get("data"),
            observed,
            canonical_source_title=canonical_source_title,
        ):
            errors.append(
                "preserved Source metadata does not match journal: "
                + str(expected.get("source_id") or "")
            )
    for expected in journal.get("preserved_source_artifacts") or []:
        row = (
            db_store.get_connection()
            .execute(
                "SELECT source_id, sha256, byte_size, mime_type, storage_uri, "
                "integrity_status, data_json FROM source_artifacts WHERE artifact_id = ?",
                (str(expected.get("artifact_id") or ""),),
            )
            .fetchone()
        )
        observed = (
            {
                "source_id": row["source_id"],
                "sha256": row["sha256"],
                "byte_size": row["byte_size"],
                "mime_type": row["mime_type"],
                "storage_uri": row["storage_uri"],
                "integrity_status": row["integrity_status"],
                "data": json.loads(row["data_json"] or "{}"),
            }
            if row is not None
            else None
        )
        expected_state = {
            field: expected.get(field)
            for field in (
                "source_id",
                "sha256",
                "byte_size",
                "mime_type",
                "storage_uri",
                "integrity_status",
                "data",
            )
        }
        if observed != expected_state:
            errors.append(
                "preserved Source artifact does not match journal: "
                + str(expected.get("artifact_id") or "")
            )

    from vector_lake import indexer

    if not indexer.index_projection_matches_canonical(
        [target_name, source_name],
        allowed_alias_redirects={
            candidate["right_page_key"]: candidate["left_page_key"]
        },
    ):
        errors.append("index projection does not match canonical merge state")
    return errors


def _strict_outbox_id_list(value, *, owner: str) -> list[int]:
    if not isinstance(value, list) or not value:
        raise RuntimeError(
            f"Merge projection control-state mismatch: {owner} outbox IDs are missing."
        )
    ids = []
    for outbox_id in value:
        if (
            isinstance(outbox_id, bool)
            or not isinstance(outbox_id, int)
            or outbox_id <= 0
        ):
            raise RuntimeError(
                f"Merge projection control-state mismatch: {owner} contains an invalid outbox ID."
            )
        ids.append(outbox_id)
    if len(ids) != len(set(ids)):
        raise RuntimeError(
            f"Merge projection control-state mismatch: {owner} contains duplicate outbox IDs."
        )
    return ids


def _validate_merge_outbox_control_state(item: dict, journal: dict) -> list[int]:
    """Bind a recoverable merge to its exact two durable projection intents."""
    item_ids = _strict_outbox_id_list(
        item.get("merge_outbox_ids"),
        owner="governance item",
    )
    journal_ids = _strict_outbox_id_list(
        journal.get("outbox_ids"),
        owner="merge journal",
    )
    if set(item_ids) != set(journal_ids):
        raise RuntimeError(
            "Merge projection control-state mismatch: governance item and journal "
            "outbox IDs differ."
        )

    required = {
        "target_filename",
        "source_filename",
        "target_version",
        "source_version",
        "target_projection_hash",
        "source_projection_hash",
        "merged_projection_hash",
    }
    missing = sorted(key for key in required if key not in journal)
    if missing:
        raise RuntimeError(
            "Merge projection control-state mismatch: journal intent metadata is "
            f"missing {', '.join(missing)}."
        )
    target_filename = str(journal["target_filename"])
    source_filename = str(journal["source_filename"])
    if not target_filename or not source_filename or target_filename == source_filename:
        raise RuntimeError(
            "Merge projection control-state mismatch: journal filenames are invalid."
        )
    expected_by_filename = {
        target_filename: {
            "mutation_type": "update",
            "validation_mode": "schema",
            "base_version": str(journal["target_version"]),
            "projection_base_hash": str(journal["target_projection_hash"]),
            "payload_hash": str(journal["merged_projection_hash"]),
        },
        source_filename: {
            "mutation_type": "delete",
            "validation_mode": "schema",
            "base_version": str(journal["source_version"]),
            "projection_base_hash": str(journal["source_projection_hash"]),
            "payload_hash": None,
        },
    }
    if len(journal_ids) != len(expected_by_filename):
        raise RuntimeError(
            "Merge projection control-state mismatch: journal must own exactly one "
            "target update and one source delete intent."
        )

    observed = db_store.mutation_outbox_intents(journal_ids)
    if set(observed) != set(journal_ids):
        raise RuntimeError(
            "Merge projection control-state mismatch: a journal outbox row is missing."
        )
    observed_filenames = set()
    for outbox_id in journal_ids:
        row = observed[outbox_id]
        filename = str(row.get("filename") or "")
        expected = expected_by_filename.get(filename)
        if expected is None or filename in observed_filenames:
            raise RuntimeError(
                "Merge projection control-state mismatch: outbox filenames do not "
                "match the journal target/source pair."
            )
        observed_filenames.add(filename)
        actual_payload_hash = (
            semantic_text_hash(str(row["payload_text"]))
            if row.get("payload_text") is not None
            else None
        )
        actual = {
            "mutation_type": str(row.get("mutation_type") or ""),
            "validation_mode": str(row.get("validation_mode") or ""),
            "base_version": str(row.get("base_version") or ""),
            "projection_base_hash": str(row.get("projection_base_hash") or ""),
            "payload_hash": actual_payload_hash,
        }
        if actual != expected:
            raise RuntimeError(
                "Merge projection control-state mismatch: outbox intent does not "
                f"match journal metadata for {filename}."
            )
    if observed_filenames != set(expected_by_filename):
        raise RuntimeError(
            "Merge projection control-state mismatch: outbox intent set is incomplete."
        )
    return journal_ids


def _reconcile_projection_pending(
    item: dict,
    *,
    allow_failed_retry: bool = False,
) -> dict:
    candidate = item["merge_candidate"]
    journal_id = str(item.get("merge_journal_id") or "")
    journal = db_store.get_merge_journal(journal_id) if journal_id else None
    if not journal:
        raise RuntimeError("Projection-pending merge is missing its recovery journal.")
    outbox_ids = _validate_merge_outbox_control_state(item, journal)

    from vector_lake.watchdog_app import process_mutation_outbox_batch

    try:
        recovery = None
        initial_statuses = db_store.mutation_outbox_statuses(outbox_ids)
        if allow_failed_retry and any(
            status == "failed" for status in initial_statuses.values()
        ):
            recovery = db_store.recover_failed_mutation_outbox(outbox_ids)
        process_mutation_outbox_batch(
            limit=max(1, len(outbox_ids)),
            max_attempts=3,
            backoff_base=0,
            outbox_ids=outbox_ids,
        )
        statuses = db_store.mutation_outbox_statuses(outbox_ids)
        terminal_projection_statuses = {"completed", "superseded"}
        if set(statuses) != set(outbox_ids) or any(
            status not in terminal_projection_statuses
            for status in statuses.values()
        ):
            pending_updates = {
                "status": "projection_pending",
                "merge_outbox_statuses": statuses,
                "last_projection_check": _utc_now(),
            }
            if recovery is not None:
                pending_updates["failed_outbox_recovery"] = recovery
            db_store.update_merge_journal(
                journal_id,
                {
                    "outbox_statuses": statuses,
                    "last_checked_at": _utc_now(),
                    **(
                        {"failed_outbox_recovery": recovery}
                        if recovery is not None
                        else {}
                    ),
                },
                status="projection_pending",
            )
            return (
                governance_store.update_governance_item(
                    item["item_id"],
                    pending_updates,
                    expected_statuses={"pending", "projection_pending"},
                )
                or item
            )

        errors = _post_merge_errors(candidate, journal)
        if errors:
            db_store.update_merge_journal(
                journal_id,
                {
                    "outbox_statuses": statuses,
                    "postcondition_errors": errors,
                    **(
                        {"failed_outbox_recovery": recovery}
                        if recovery is not None
                        else {}
                    ),
                },
                status="verification_failed",
            )
            verification_updates = {
                "status": "projection_pending",
                "merge_outbox_statuses": statuses,
                "postcondition_errors": errors,
                "last_projection_check": _utc_now(),
            }
            if recovery is not None:
                verification_updates["failed_outbox_recovery"] = recovery
            return (
                governance_store.update_governance_item(
                    item["item_id"],
                    verification_updates,
                    expected_statuses={"pending", "projection_pending"},
                )
                or item
            )

        db_store.update_merge_journal(
            journal_id,
            {
                "outbox_statuses": statuses,
                "completed_at": _utc_now(),
                **(
                    {"failed_outbox_recovery": recovery}
                    if recovery is not None
                    else {}
                ),
            },
            status="completed",
        )
        resolved_updates = {
            "status": "resolved",
            "resolution": "merge",
            "merge_outbox_statuses": statuses,
            "postcondition_errors": [],
            "resolved_at": _utc_now(),
        }
        if recovery is not None:
            resolved_updates["failed_outbox_recovery"] = recovery
        return (
            governance_store.update_governance_item(
                item["item_id"],
                resolved_updates,
                expected_statuses={"pending", "projection_pending"},
            )
            or item
        )
    except Exception as exc:
        return {
            **item,
            "status": "projection_pending",
            "recovery_required": True,
            "last_projection_error": f"{type(exc).__name__}: {exc}",
        }


def _record_merge_post_commit_state(
    journal_id: str,
    pending_item: dict,
    deferred: list[str],
) -> dict | None:
    """Record best-effort post-commit detail without redefining the commit."""
    try:
        db_store.update_merge_journal(
            journal_id,
            {"deferred": list(deferred)},
            status="projection_pending",
        )
    except Exception as exc:
        return {
            **pending_item,
            "status": "projection_pending",
            "recovery_required": True,
            "last_projection_error": (
                "Post-commit merge journal update failed: "
                f"{type(exc).__name__}: {exc}"
            ),
        }
    return None


def resolve_governance_item(
    item_id: str,
    resolution: str = "skip",
    change_manifest: dict | None = None,
) -> dict | None:
    item = governance_store.get_governance_item(item_id)
    if item is None:
        return None
    status = str(item.get("status") or "")
    if resolution != "merge" or item.get("type") != "merge":
        if status != "pending":
            return None
        return governance_store.update_governance_item(
            item_id,
            {
                "status": "resolved",
                "resolution": resolution,
                "resolved_at": _utc_now(),
            },
            expected_statuses={"pending"},
        )
    if status == "resolved":
        return item
    if status == "projection_pending":
        return _reconcile_projection_pending(item, allow_failed_retry=True)
    if status != "pending":
        return None

    candidate = item.get("merge_candidate")
    if not isinstance(candidate, dict) or not candidate:
        raise RuntimeError(
            "Missing merge candidate must be regenerated before resolution."
        )
    required_contract_fields = (
        "left_entity_id",
        "right_entity_id",
        "left_name",
        "right_name",
        "left_page_key",
        "right_page_key",
        "left_version",
        "right_version",
        "left_projection_hash",
        "right_projection_hash",
    )
    if (
        candidate.get("decision") != "merge"
        or candidate.get("preflight_state") != "passed"
        or any(not candidate.get(field) for field in required_contract_fields)
    ):
        raise RuntimeError(
            "Legacy or incomplete merge candidate must be regenerated with decision, "
            "preflight, canonical versions, and projection hashes."
        )

    wiki_dir = get_wiki_dir().resolve()
    candidate = preflight_suggestion(candidate, wiki_dir)
    if candidate.get("preflight_state") != "passed":
        raise RuntimeError(
            "Merge preflight must pass immediately before mutation: "
            + "; ".join(candidate.get("preflight_errors") or [])
        )
    item["merge_candidate"] = candidate

    left_id = str(candidate["left_entity_id"])
    right_id = str(candidate["right_entity_id"])
    _validate_alias_redirect(right_id, left_id)
    old_left_entity = governance_store.get_entity(left_id)
    if old_left_entity and old_left_entity.get("merged_into") == right_id:
        raise RuntimeError("Target is already merged into Source.")
    if change_manifest and change_manifest.get("allow_cycles") is True:
        raise RuntimeError("Alias-cycle validation cannot be disabled.")

    left_path = _find_md_file(
        wiki_dir, candidate["left_page_key"], candidate["left_name"]
    )
    right_path = _find_md_file(
        wiki_dir, candidate["right_page_key"], candidate["right_name"]
    )
    if not left_path or not right_path or left_path == right_path:
        raise RuntimeError("Semantic merge requires two distinct existing wiki pages.")

    left_bytes = left_path.read_bytes()
    right_bytes = right_path.read_bytes()
    if (
        hashlib.sha256(left_bytes).hexdigest() != candidate["left_projection_hash"]
        or hashlib.sha256(right_bytes).hexdigest() != candidate["right_projection_hash"]
    ):
        raise RuntimeError(
            "Markdown projection changed after merge preflight; regenerate the candidate."
        )
    left_content = normalize_semantic_text(left_bytes.decode("utf-8"))
    right_content = normalize_semantic_text(right_bytes.decode("utf-8"))
    merged_content = merge_markdown_content(
        left_content,
        right_content,
        source_key=candidate["right_page_key"],
        source_metadata_conflict_policy=candidate.get(
            "source_metadata_conflict_policy"
        ),
    )
    merged_projection_hash = semantic_text_hash(merged_content)
    journal_id = (
        "merge_"
        + hashlib.sha256(
            f"{item_id}\0{candidate['pair_key']}".encode("utf-8")
        ).hexdigest()[:24]
    )
    source_rows = _source_rows_snapshot(
        {left_path.name, right_path.name},
        str(candidate.get("source_identity") or ""),
    )
    source_artifacts = _source_artifacts_snapshot(source_rows)
    journal_snapshot = {
        "target_filename": left_path.name,
        "source_filename": right_path.name,
        "target_content": left_content,
        "source_content": right_content,
        "target_version": candidate["left_version"],
        "source_version": candidate["right_version"],
        "target_projection_hash": candidate["left_projection_hash"],
        "source_projection_hash": candidate["right_projection_hash"],
        "merged_projection_hash": merged_projection_hash,
        "candidate": candidate,
        "source_rows": source_rows,
        "source_artifacts": source_artifacts,
        "alias_snapshot": {
            left_id: governance_store.get_alias(left_id),
            right_id: governance_store.get_alias(right_id),
        },
        "backlink_manifest": candidate.get("backlink_manifest") or [],
    }

    pending_holder: dict[str, dict] = {}

    def verify_source_metadata_snapshot():
        if (
            hashlib.sha256(left_path.read_bytes()).hexdigest()
            != candidate["left_projection_hash"]
            or hashlib.sha256(right_path.read_bytes()).hexdigest()
            != candidate["right_projection_hash"]
        ):
            raise RuntimeError(
                "Markdown projection changed before canonical merge commit; regenerate the candidate."
            )
        backlinks = _direct_backlinks(
            candidate["right_page_key"],
            {left_path.name, right_path.name},
        )
        if backlinks:
            raise RuntimeError(
                "Source merge gained direct backlinks before commit: "
                + ", ".join(backlinks[:10])
            )
        raw_identity = str(candidate.get("source_identity") or "")
        if raw_identity and _current_raw_artifact_hash(raw_identity) != candidate.get(
            "source_artifact_hash"
        ):
            raise RuntimeError(
                f"Source raw artifact changed before canonical merge commit: {raw_identity}"
            )
        current_rows = _source_rows_snapshot(
            {left_path.name, right_path.name},
            str(candidate.get("source_identity") or ""),
        )
        current_artifacts = _source_artifacts_snapshot(current_rows)
        if current_rows != source_rows or current_artifacts != source_artifacts:
            raise RuntimeError(
                "Canonical Source metadata changed after merge preflight; regenerate the candidate."
            )

    def commit_merge_control_state(outbox_ids: list[int]):
        db_store.record_merge_journal(journal_id, item_id, journal_snapshot)
        preserved_rows, preserved_artifacts = _preserve_source_metadata(
            source_rows,
            source_artifacts,
            str(candidate.get("source_identity") or ""),
            left_path.name,
        )
        governance_store.upsert_alias(right_id, left_id)
        db_store.update_merge_journal(
            journal_id,
            {
                "outbox_ids": list(outbox_ids),
                "committed_at": _utc_now(),
                "preserved_source_rows": preserved_rows,
                "preserved_source_artifacts": preserved_artifacts,
            },
            status="projection_pending",
        )
        pending_item = governance_store.update_governance_item(
            item_id,
            {
                "status": "projection_pending",
                "merge_candidate": candidate,
                "merge_journal_id": journal_id,
                "merge_outbox_ids": list(outbox_ids),
                "merge_committed_at": _utc_now(),
            },
            expected_statuses={"pending"},
        )
        if pending_item is None:
            raise RuntimeError(
                "Governance merge state changed before the canonical transaction committed."
            )
        pending_holder["item"] = pending_item

    outcome = execute_mutation_batch(
        [
            {
                "filename": left_path.name,
                "content": merged_content,
                "expected_version": candidate["left_version"],
                "expected_projection_hash": candidate["left_projection_hash"],
            },
            {
                "filename": right_path.name,
                "is_delete": True,
                "expected_version": candidate["right_version"],
                "expected_projection_hash": candidate["right_projection_hash"],
            },
        ],
        validation_mode="schema",
        origin="governance-merge",
        return_details=True,
        transaction_callback=commit_merge_control_state,
        precondition_callback=verify_source_metadata_snapshot,
    )
    pending_item = pending_holder.get("item") or governance_store.get_governance_item(
        item_id
    )
    if not pending_item or pending_item.get("status") != "projection_pending":
        raise RuntimeError(
            "Committed merge is missing its projection-pending control state."
        )
    post_commit_pending = _record_merge_post_commit_state(
        journal_id,
        pending_item,
        outcome["deferred"],
    )
    if post_commit_pending is not None:
        return post_commit_pending
    return _reconcile_projection_pending(pending_item)
