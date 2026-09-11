"""Fail-closed retention of reviewed official provenance during canonical apply."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from vector_lake.wiki_utils import get_memory_dir
from vector_lake.governance_metrics import claim_governance_version


_REPAIR_CONTRACT = "claim-provenance-repair-plan-v1"
_EVIDENCE_TYPE = "operator-reviewed-official-excerpt"
_MAX_ARTIFACTS = 64
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_EXCERPT_BYTES = 64 * 1024
_MAX_RETENTION_CLAIMS = 256
_MAX_RETENTION_EVIDENCE = 512
_MAX_ASSESSMENTS_PER_CLAIM = 256
_WITHDRAWN = {"withdrawn", "rejected", "revoked", "expired", "archived",
              "retired", "decayed", "inactive", "superseded", "invalid"}


class ReviewedProvenanceRetentionError(ValueError):
    """Current reviewed provenance could not be safely verified."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _identity(record: dict) -> tuple:
    locator = record.get("locator") or {}
    return (
        record.get("claim_id"), record.get("claim_text"), locator.get("page_key"),
        record.get("claim_type", record.get("type")), record.get("claim_scope"),
        record.get("source_page"),
        tuple(sorted(str(item) for item in record.get("subject_entity_ids") or [])),
    )


def _inactive(record: dict) -> bool:
    values = [record.get("status"), record.get("validity_state"), record.get("lifecycle_state")]
    if any(str(value or "").strip().lower() in _WITHDRAWN for value in values):
        return True
    for key in ("valid_to", "expires_at", "expiry"):
        value = record.get(key)
        if value is None:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError
        except (AttributeError, TypeError, ValueError) as exc:
            raise ReviewedProvenanceRetentionError(
                "Reviewed provenance has invalid lifecycle metadata."
            ) from exc
        if parsed <= datetime.now(timezone.utc):
            return True
    return False


def _rows(conn, table: str, id_field: str, ids: set[str]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    ordered = sorted(ids)
    for offset in range(0, len(ordered), 500):
        batch = ordered[offset:offset + 500]
        marks = ",".join("?" for _ in batch)
        physical = ", source_id" if table == "source_artifacts" else ""
        for row in conn.execute(
            f"SELECT {id_field}, data_json{physical} FROM {table} WHERE {id_field} IN ({marks})",
            tuple(batch),
        ):
            physical_id = str(row[id_field])
            try:
                item = json.loads(row["data_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise ReviewedProvenanceRetentionError(
                    "Reviewed provenance canonical row is malformed."
                ) from exc
            if not isinstance(item, dict) or str(item.get(id_field)) != physical_id:
                raise ReviewedProvenanceRetentionError(
                    "Reviewed provenance canonical row ID mismatch."
                )
            if table == "source_artifacts" and str(item.get("source_id")) != str(row["source_id"]):
                raise ReviewedProvenanceRetentionError(
                    "Reviewed provenance artifact/source physical binding mismatch."
                )
            result[physical_id] = item
    return result


def _expiry_or_revocation(record: dict) -> bool:
    snapshot = record.get("official_snapshot")
    candidates = [record, record.get("metadata"), snapshot,
                  snapshot.get("metadata") if isinstance(snapshot, dict) else None]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if candidate.get("revoked_at") is not None:
            return True
        if candidate.get("expires_at") is not None:
            # Producer already requires an ISO timestamp. Lexical UTC comparison is
            # insufficient here; reuse datetime parsing without accepting dates.
            try:
                expires = datetime.fromisoformat(str(candidate["expires_at"]).replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise ReviewedProvenanceRetentionError(
                    "Reviewed provenance has invalid expiry metadata."
                ) from exc
            if expires <= datetime.now(timezone.utc):
                return True
    return False


def _receipt(evidence: dict, run: dict, claim: dict) -> dict:
    review = evidence.get("official_review")
    receipt = run.get("review_receipt")
    if not isinstance(review, dict) or not isinstance(receipt, dict):
        raise ReviewedProvenanceRetentionError(
            "Current reviewed provenance is missing its producer receipt preimage."
        )
    expected = _sha({key: value for key, value in receipt.items()
                     if key != "review_receipt_sha256"})
    declared = receipt.get("review_receipt_sha256")
    if not isinstance(declared, str) or declared != expected:
        raise ReviewedProvenanceRetentionError("Reviewed provenance receipt hash mismatch.")
    if run.get("review_receipt_sha256") != expected or review.get("receipt_sha256") != expected:
        raise ReviewedProvenanceRetentionError("Reviewed provenance receipt bindings disagree.")
    if receipt.get("claim_id") != claim.get("claim_id"):
        raise ReviewedProvenanceRetentionError("Reviewed provenance receipt claim mismatch.")
    return receipt


def _negative_assessment(conn, claim: dict) -> bool:
    rows = conn.execute(
        "SELECT data_json FROM claim_assessments WHERE claim_id=? AND outcome IN (?, ?) "
        "LIMIT ?",
        (claim.get("claim_id"), "unsupported", "contradicted",
         _MAX_ASSESSMENTS_PER_CLAIM + 1),
    )
    version = claim_governance_version(claim)
    found = False
    for index, row in enumerate(rows):
        if index == _MAX_ASSESSMENTS_PER_CLAIM:
            raise ReviewedProvenanceRetentionError(
                "Current claim assessment history budget exceeded."
            )
        try:
            assessment = json.loads(row["data_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ReviewedProvenanceRetentionError(
                "Current claim assessment ledger is malformed."
            ) from exc
        if isinstance(assessment, dict) and assessment.get("claim_version") == version:
            found = True
    return found


def _file_signature(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _unsafe_node(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & 0x400
    )


def _validated_raw_path(memory: Path, raw_ref: str) -> Path:
    """Validate every lexical node before resolving or opening the raw file."""
    relative = Path(raw_ref)
    path = memory / relative
    node = memory
    try:
        for part in relative.parts:
            node = node / part
            if _unsafe_node(node.lstat()):
                raise ReviewedProvenanceRetentionError(
                    "Reviewed provenance raw path contains an unsafe link or reparse point."
                )
        resolved_memory = memory.resolve()
        research = (memory / "raw" / "research").resolve()
        resolved = path.resolve()
        if (not research.is_relative_to(resolved_memory)
                or not resolved.is_relative_to(research) or not resolved.is_file()):
            raise ReviewedProvenanceRetentionError(
                "Reviewed provenance raw snapshot is missing."
            )
    except OSError as exc:
        raise ReviewedProvenanceRetentionError(
            "Reviewed provenance raw snapshot is unreadable."
        ) from exc
    return path


def _stream_verify(path: Path, excerpt: bytes, expected_size: int,
                   total_read: list[int]) -> tuple[str, bool, tuple[int, int, int, int]]:
    if not excerpt:
        raise ReviewedProvenanceRetentionError(
            "Reviewed provenance excerpt must be non-empty."
        )
    digest, overlap, found, read = hashlib.sha256(), b"", False, 0
    try:
        lexical_before = path.stat()
        with path.open("rb") as handle:
            descriptor_before = os.fstat(handle.fileno())
            if _file_signature(descriptor_before) != _file_signature(lexical_before):
                raise ReviewedProvenanceRetentionError(
                    "Reviewed provenance raw snapshot changed before verification."
                )
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                read += len(chunk)
                total_read[0] += len(chunk)
                if read > _MAX_FILE_BYTES or total_read[0] > _MAX_TOTAL_BYTES:
                    raise ReviewedProvenanceRetentionError(
                        "Reviewed provenance byte budget exceeded."
                    )
                digest.update(chunk)
                found = found or excerpt in overlap + chunk
                overlap = ((overlap + chunk)[-(len(excerpt) - 1):]
                           if len(excerpt) > 1 else b"")
            descriptor_after = os.fstat(handle.fileno())
        lexical_after = path.stat()
    except OSError as exc:
        raise ReviewedProvenanceRetentionError(
            "Reviewed provenance raw snapshot is unreadable."
        ) from exc
    signatures = {_file_signature(item) for item in
                  (lexical_before, descriptor_before, descriptor_after, lexical_after)}
    if len(signatures) != 1:
        raise ReviewedProvenanceRetentionError(
            "Reviewed provenance raw snapshot changed during verification."
        )
    if read != expected_size:
        raise ReviewedProvenanceRetentionError(
            "Reviewed provenance raw snapshot size changed during verification."
        )
    return digest.hexdigest(), found, _file_signature(lexical_after)


def _unique_by_id(records: list[dict], key: str) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for item in records:
        value = str(item.get(key))
        if value in result:
            raise ReviewedProvenanceRetentionError(
                f"Incoming reviewed provenance contains duplicate {key}."
            )
        result[value] = item
    return result


def _verify_preimage(conn, claim: dict, receipt: dict, family_resolver) -> dict:
    expected_hash = receipt.get("expected_claim_sha256")
    expected_version = receipt.get("expected_claim_version")
    family = str(family_resolver(claim) or "")
    if not family or not isinstance(expected_hash, str) or not expected_hash:
        raise ReviewedProvenanceRetentionError(
            "Current reviewed provenance lacks an indexed claim preimage binding."
        )
    row = conn.execute(
        "SELECT data_json FROM claim_versions WHERE claim_family_id=? AND record_hash=?",
        (family, expected_hash),
    ).fetchone()
    if row is None:
        raise ReviewedProvenanceRetentionError(
            "Current reviewed provenance claim preimage is unavailable."
        )
    preimage = json.loads(row["data_json"])
    if _sha(preimage) != expected_hash or claim_governance_version(preimage) != expected_version:
        raise ReviewedProvenanceRetentionError(
            "Current reviewed provenance claim preimage/version mismatch."
        )
    if _identity(preimage) != _identity(claim):
        raise ReviewedProvenanceRetentionError(
            "Current reviewed provenance crossed its reviewed semantic boundary."
        )
    return preimage


def retain_current_reviewed_provenance(
    conn, *, old_claims: list[dict], old_evidence: list[dict],
    proposed_claims: list[dict], proposed_evidence: list[dict],
    proposed_sources: list[dict], proposed_artifacts: list[dict],
    proposed_runs: list[dict], affected_page_keys: set[str], family_resolver,
) -> tuple[list[dict], list[dict]]:
    """Return proposals augmented only with fully reverified current bindings."""
    proposed_by_id = _unique_by_id(proposed_claims, "claim_id")
    evidence_by_id = _unique_by_id(old_evidence, "evidence_id")
    proposed_evidence_by_id = _unique_by_id(proposed_evidence, "evidence_id")
    candidates: list[tuple[dict, dict, list[str]]] = []
    evidence_ids: set[str] = set()
    candidate_claim_ids: set[str] = set()
    for current in old_claims:
        marker = current.get("provenance_repair")
        governed = [str(item) for item in current.get("evidence_ids") or []]
        if not (isinstance(marker, dict) and marker.get("contract") == _REPAIR_CONTRACT and governed):
            continue
        proposed = proposed_by_id.get(str(current.get("claim_id")))
        if (proposed is None or _inactive(current) or _inactive(proposed)
                or _expiry_or_revocation(current) or _expiry_or_revocation(proposed)
                or _identity(current) != _identity(proposed)):
            continue
        missing = [eid for eid in governed if eid not in evidence_by_id]
        supported = [eid for eid in governed
                     if evidence_by_id.get(eid, {}).get("evidence_type") == _EVIDENCE_TYPE]
        if missing:
            raise ReviewedProvenanceRetentionError(
                "Current governed repair references missing canonical reviewed evidence."
            )
        if len(supported) != len(governed):
            raise ReviewedProvenanceRetentionError(
                "Current governed repair contains an unprovable evidence marker."
            )
        if len(set(governed)) != len(governed):
            raise ReviewedProvenanceRetentionError(
                "Current governed repair contains duplicate reviewed evidence IDs."
            )
        if _negative_assessment(conn, current):
            continue
        current_id = str(current.get("claim_id"))
        if current_id in candidate_claim_ids:
            raise ReviewedProvenanceRetentionError(
                "Current reviewed provenance contains an ambiguous retention group."
            )
        candidate_claim_ids.add(current_id)
        candidates.append((current, proposed, supported))
        evidence_ids.update(supported)
        if len(candidates) > _MAX_RETENTION_CLAIMS:
            raise ReviewedProvenanceRetentionError(
                "Reviewed provenance eligible claim budget exceeded."
            )
        if len(evidence_ids) > _MAX_RETENTION_EVIDENCE:
            raise ReviewedProvenanceRetentionError(
                "Reviewed provenance evidence budget exceeded."
            )
    if not candidates:
        return proposed_claims, proposed_evidence
    current_evidence = _rows(conn, "evidence", "evidence_id", evidence_ids)
    source_ids = {str(item.get("source_id")) for item in current_evidence.values()}
    artifact_ids = {str(item.get("artifact_id")) for item in current_evidence.values()}
    run_ids = {str(item.get("extraction_run_id")) for item in current_evidence.values()}
    if len(artifact_ids) > _MAX_ARTIFACTS:
        raise ReviewedProvenanceRetentionError("Reviewed provenance artifact budget exceeded.")
    sources = _rows(conn, "sources", "source_id", source_ids)
    artifacts = _rows(conn, "source_artifacts", "artifact_id", artifact_ids)
    runs = _rows(conn, "extraction_runs", "run_id", run_ids)
    incoming = {}
    for records, key in ((proposed_sources, "source_id"),
                         (proposed_artifacts, "artifact_id"),
                         (proposed_runs, "run_id")):
        for item in records:
            incoming_key = (key, str(item.get(key)))
            if incoming_key in incoming:
                raise ReviewedProvenanceRetentionError(
                    f"Incoming reviewed provenance contains duplicate {key}."
                )
            incoming[incoming_key] = item
    retained: dict[str, dict] = {}
    cache: dict[tuple[str, bytes], tuple[str, bool, tuple[int, int, int, int]]] = {}
    total_read = [0]
    for current, proposed, ids in candidates:
        validated: list[str] = []
        validated_sources: list[str] = []
        for eid in ids:
            evidence = current_evidence.get(eid)
            if evidence is None:
                raise ReviewedProvenanceRetentionError("Current reviewed evidence is missing.")
            source_id = str(evidence.get("source_id"))
            artifact_id = str(evidence.get("artifact_id"))
            run_id = str(evidence.get("extraction_run_id"))
            if sources.get(source_id) is None or artifacts.get(artifact_id) is None or runs.get(run_id) is None:
                raise ReviewedProvenanceRetentionError("Current reviewed provenance foundation is missing.")
            source, artifact, run = sources[source_id], artifacts[artifact_id], runs[run_id]
            replacements = [(source, incoming.get(("source_id", source_id))),
                            (artifact, incoming.get(("artifact_id", artifact_id))),
                            (run, incoming.get(("run_id", run_id)))]
            if any(new is not None and (_inactive(new) or _expiry_or_revocation(new))
                   for _, new in replacements):
                continue
            if any(new is not None and new != old for old, new in replacements):
                raise ReviewedProvenanceRetentionError(
                    "Incoming provenance foundation conflicts with current reviewed proof."
                )
            if (any(_inactive(item) or _expiry_or_revocation(item)
                    for item in (evidence, source, artifact, run))
                    or artifact.get("integrity_status") != "verified"):
                continue
            if (artifact.get("source_id") != source_id
                    or source.get("artifact_id") != artifact_id):
                raise ReviewedProvenanceRetentionError(
                    "Reviewed provenance cross-source artifact binding mismatch."
                )
            receipt = _receipt(evidence, run, current)
            from vector_lake import tool_claim_provenance as producer
            if (run.get("extractor_name") != producer._EXTRACTOR_NAME or
                    run.get("extractor_version") != producer._EXTRACTOR_VERSION):
                raise ReviewedProvenanceRetentionError(
                    "Reviewed provenance producer identity mismatch."
                )
            reviewed_preimage = _verify_preimage(conn, current, receipt, family_resolver)
            review = evidence["official_review"]
            snapshot = source.get("official_snapshot")
            if not isinstance(snapshot, dict) or review.get("claim_id") != current.get("claim_id"):
                raise ReviewedProvenanceRetentionError("Reviewed provenance snapshot binding mismatch.")
            if review.get("review") != receipt.get("review"):
                raise ReviewedProvenanceRetentionError("Reviewed provenance operator review mismatch.")
            if review.get("claim_version") != receipt.get("expected_claim_version"):
                raise ReviewedProvenanceRetentionError("Reviewed provenance claim version binding mismatch.")
            if (review.get("source_id") != source.get("source_id") or
                    evidence.get("source_id") != source.get("source_id") or
                    evidence.get("artifact_id") != artifact.get("artifact_id") or
                    evidence.get("extraction_run_id") != run.get("run_id") or
                    source.get("source_id") not in (current.get("source_ids") or []) or
                    artifact.get("artifact_id") not in (run.get("source_artifact_ids") or [])):
                raise ReviewedProvenanceRetentionError("Reviewed provenance physical ID mismatch.")
            attestation = review.get("semantic_review")
            if not isinstance(attestation, dict) or attestation.get("supports_claim") is not True or attestation.get("original_source_verified") is not True:
                raise ReviewedProvenanceRetentionError("Reviewed provenance semantic attestation is invalid.")
            if snapshot.get("semantic_review") not in (None, attestation):
                raise ReviewedProvenanceRetentionError("Reviewed provenance semantic review bindings disagree.")
            receipt_snapshots = receipt.get("snapshots")
            reconstructed = copy.deepcopy(snapshot)
            reconstructed["quoted_excerpt"] = evidence.get("evidence_text")
            reconstructed["semantic_review"] = copy.deepcopy(attestation)
            if (not isinstance(receipt_snapshots, list) or
                    reconstructed not in receipt_snapshots):
                raise ReviewedProvenanceRetentionError(
                    "Reviewed provenance receipt snapshot mismatch."
                )
            from vector_lake.evidence_foundation import build_extraction_run
            reviewed_page = producer._claim_page_key(current)
            if run.get("page_key") != reviewed_page:
                raise ReviewedProvenanceRetentionError(
                    "Reviewed provenance producer page binding mismatch."
                )
            base_run = build_extraction_run(
                page_key=reviewed_page, body=evidence.get("evidence_text"),
                artifact_ids=[artifact_id], frontmatter={},
                extractor_name=producer._EXTRACTOR_NAME,
                extractor_version=producer._EXTRACTOR_VERSION,
            )
            if any(run.get(key) != value for key, value in base_run.items() if key != "run_id"):
                raise ReviewedProvenanceRetentionError(
                    "Reviewed provenance producer descriptor mismatch."
                )
            expected_run = producer._claim_stable_id(
                "extractrun", base_run["run_id"] + receipt["review_receipt_sha256"]
                + str(current.get("claim_id"))
            )
            expected_evidence = producer._claim_stable_id(
                "evidence", "official:" + str(current.get("claim_id"))
                + receipt["review_receipt_sha256"] + _sha(reconstructed)
            )
            if run_id != expected_run or eid != expected_evidence:
                raise ReviewedProvenanceRetentionError(
                    "Reviewed provenance deterministic producer ID mismatch."
                )
            from vector_lake.evidence_foundation import source_locator_for, evidence_independence
            raw_ref = snapshot.get("raw_ref")
            expected_locator = copy.deepcopy(
                reviewed_preimage.get("locator") or {"page_key": reviewed_page}
            )
            expected_source_locator = {
                **source_locator_for({}, raw_ref, artifact={
                    "artifact_id": artifact_id,
                    "content_hash": snapshot.get("raw_sha256"),
                    "integrity_status": "verified",
                }),
                "original_url": snapshot.get("original_url"),
                "representation": snapshot.get("representation"),
                "quoted_excerpt_sha256": hashlib.sha256(evidence["evidence_text"].encode("utf-8")).hexdigest(),
            }
            expected_descriptor = {
                "evidence_family_id": eid,
                "locator": expected_locator,
                "projection_locator": expected_locator,
                "source_locator": expected_source_locator,
                "evidence_type": _EVIDENCE_TYPE,
                "official_review": {
                    "claim_id": current["claim_id"],
                    "claim_version": receipt["expected_claim_version"],
                    "source_id": source_id,
                    "receipt_sha256": receipt["review_receipt_sha256"],
                    "review": receipt["review"],
                    "semantic_review": attestation,
                },
                **evidence_independence(
                    raw_ref, f"{reviewed_page}.md",
                    (snapshot.get("metadata") or {}).get("generation_parent_refs") or [],
                ),
            }
            if any(evidence.get(key) != value for key, value in expected_descriptor.items()):
                raise ReviewedProvenanceRetentionError(
                    "Reviewed provenance evidence descriptor mismatch."
                )
            if not isinstance(raw_ref, str) or not raw_ref.startswith("raw/research/") or "\\" in raw_ref or ".." in Path(raw_ref).parts:
                raise ReviewedProvenanceRetentionError("Reviewed provenance raw path is outside MEMORY/raw/research.")
            memory = get_memory_dir()
            path = _validated_raw_path(memory, raw_ref)
            excerpt = evidence.get("evidence_text")
            if not isinstance(excerpt, str) or not excerpt or len(excerpt.encode("utf-8")) > _MAX_EXCERPT_BYTES:
                raise ReviewedProvenanceRetentionError("Reviewed provenance excerpt budget exceeded.")
            expected_size = snapshot.get("byte_size")
            if type(expected_size) is not int:
                raise ReviewedProvenanceRetentionError("Reviewed provenance snapshot size is invalid.")
            excerpt_bytes = excerpt.encode("utf-8")
            cache_key = (str(path), excerpt_bytes)
            if cache_key not in cache:
                digest, found, signature = _stream_verify(
                    path, excerpt_bytes, expected_size, total_read
                )
                cache[cache_key] = (digest, found, signature)
            else:
                digest, found, signature = cache[cache_key]
                try:
                    if _file_signature(path.stat()) != signature:
                        raise ReviewedProvenanceRetentionError(
                            "Reviewed provenance raw snapshot changed after verification."
                        )
                except OSError as exc:
                    raise ReviewedProvenanceRetentionError(
                        "Reviewed provenance raw snapshot is unreadable."
                    ) from exc
            if (snapshot.get("raw_sha256") != digest or artifact.get("content_hash") != digest
                    or artifact.get("byte_size") != expected_size or not found):
                continue
            if evidence.get("supports_claim_ids") != [current["claim_id"]] or evidence.get("contradicts_claim_ids"):
                raise ReviewedProvenanceRetentionError("Reviewed evidence support links are not exact.")
            existing = proposed_evidence_by_id.get(eid)
            if existing is not None and existing != evidence:
                raise ReviewedProvenanceRetentionError("Incoming evidence ID conflicts with current reviewed evidence.")
            retained[eid] = copy.deepcopy(evidence)
            validated.append(eid)
            validated_sources.append(str(source["source_id"]))
        if validated:
            proposed["evidence_ids"] = sorted(set(proposed.get("evidence_ids") or []) | set(validated))
            proposed["source_ids"] = sorted(set(proposed.get("source_ids") or []) | set(validated_sources))
            proposed["provenance_repair"] = copy.deepcopy(current["provenance_repair"])
            proposed.pop("assessment_status", None)
    final_evidence = list(proposed_evidence)
    present = {str(item.get("evidence_id")) for item in final_evidence}
    final_evidence.extend(record for eid, record in retained.items() if eid not in present)
    return proposed_claims, final_evidence
