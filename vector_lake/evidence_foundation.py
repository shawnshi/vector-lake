"""Evidence-foundation helpers for auditable Source -> Evidence -> Claim extraction.

The helpers in this module are deliberately conservative: a source is only
marked as integrity-verified when Vector Lake can read the referenced bytes
inside the configured MEMORY root.  Missing provenance stays explicit rather
than being replaced with a deterministic-looking placeholder.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path
from typing import Any

from vector_lake.wiki_utils import get_memory_dir, normalize_raw_ref


EXTRACTOR_NAME = "vector_lake.claim_extractor"
EXTRACTOR_VERSION = "2.0"
PARSER_NAME = "mistune-ast"


def _digest(prefix: str, value: str) -> str:
    return f"{prefix}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


def _candidate_source_path(raw_ref: str) -> Path | None:
    """Resolve a raw reference without allowing reads outside MEMORY."""
    memory_dir = get_memory_dir().resolve()
    normalized = normalize_raw_ref(raw_ref)
    if not normalized:
        return None

    raw_path = Path(normalized)
    if raw_path.is_absolute():
        candidate = raw_path.resolve()
    elif normalized.startswith("raw/"):
        candidate = (memory_dir / normalized).resolve()
    else:
        candidate = (memory_dir / normalized).resolve()
    if not candidate.is_relative_to(memory_dir):
        return None
    return candidate


def resolve_source_artifact(
    raw_ref: str,
    *,
    source_id: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe one source artifact and verify its bytes when available."""
    metadata = dict(metadata or {})
    normalized = normalize_raw_ref(raw_ref)
    candidate = _candidate_source_path(normalized)
    sha256: str | None = None
    byte_size: int | None = None
    storage_uri: str | None = None
    integrity_status = "unverified"
    if candidate is not None and candidate.is_file():
        digest = hashlib.sha256()
        byte_size = 0
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                byte_size += len(chunk)
        sha256 = digest.hexdigest()
        storage_uri = candidate.as_uri()
        integrity_status = "verified"

    artifact_basis = sha256 or f"unverified:{normalized}"
    artifact_id = _digest("artifact", artifact_basis)
    mime_type = metadata.get("mime_type") or mimetypes.guess_type(normalized)[0]
    parent_refs = metadata.get("generation_parent_refs") or metadata.get("derived_from") or []
    if isinstance(parent_refs, str):
        parent_refs = [parent_refs]
    parent_refs = [str(item).strip() for item in parent_refs if str(item).strip()]
    return {
        "artifact_id": artifact_id,
        "source_id": source_id,
        "raw_ref": normalized,
        "sha256": sha256,
        "content_hash": sha256,
        "hash_algorithm": "sha256" if sha256 else None,
        "byte_size": byte_size,
        "mime_type": mime_type,
        "storage_uri": storage_uri,
        "integrity_status": integrity_status,
        "classification": metadata.get("classification") or "unspecified",
        "retention_policy": metadata.get("retention_policy") or "unspecified",
        "legal_hold": bool(metadata.get("legal_hold", False)),
        "lineage_id": metadata.get("lineage_id") or _digest("lineage", artifact_basis),
        "generation_parent_refs": parent_refs,
    }


def build_extraction_run(
    *,
    page_key: str,
    body: str,
    artifact_ids: list[str],
    frontmatter: dict[str, Any],
    extractor_name: str = EXTRACTOR_NAME,
    extractor_version: str = EXTRACTOR_VERSION,
) -> dict[str, Any]:
    """Build an idempotent extraction-run descriptor for one page revision."""
    contract = {
        "extractor_name": str(extractor_name),
        "extractor_version": str(extractor_version),
        "parser_name": PARSER_NAME,
        "parser_version": str(frontmatter.get("parser_version") or "runtime"),
        "model_name": frontmatter.get("model_name"),
        "model_version": frontmatter.get("model_version"),
        "prompt_version": frontmatter.get("prompt_version"),
    }
    input_fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()
    run_basis = json.dumps(
        {
            "page_key": page_key,
            "input_fingerprint": input_fingerprint,
            "artifact_ids": sorted(artifact_ids),
            "contract": contract,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "run_id": _digest("extractrun", run_basis),
        "page_key": page_key,
        "input_fingerprint": input_fingerprint,
        "source_artifact_ids": sorted(set(artifact_ids)),
        **contract,
    }


def source_locator_for(
    frontmatter: dict[str, Any],
    raw_ref: str,
) -> dict[str, Any]:
    """Return a raw-source locator, never a Wiki projection locator."""
    locators = frontmatter.get("source_locators") or {}
    locator: Any = None
    if isinstance(locators, dict):
        locator = locators.get(raw_ref) or locators.get(normalize_raw_ref(raw_ref))
    if isinstance(locator, dict) and locator:
        return {str(key): value for key, value in locator.items()}
    return {
        "kind": "unresolved",
        "raw_ref": normalize_raw_ref(raw_ref),
    }


def evidence_independence(raw_ref: str, page_name: str, parent_refs: list[str]) -> dict[str, Any]:
    """Flag projection/self-derived evidence so consumers cannot treat it as independent."""
    normalized = normalize_raw_ref(raw_ref)
    page_key = Path(page_name).stem
    ref_stem = Path(normalized).stem
    self_reference = ref_stem == page_key or normalized.endswith(page_name)
    derived = bool(parent_refs)
    if self_reference:
        status = "projection_self_reference"
    elif derived:
        status = "derived_source"
    else:
        status = "independent_or_unknown"
    return {
        "independence_status": status,
        "lineage_safe": not self_reference,
    }


def version_family_id(prefix: str, page_key: str, locator: dict[str, Any]) -> str:
    basis = json.dumps(
        {
            "page_key": page_key,
            "heading": locator.get("heading"),
            "block_index": locator.get("block_index"),
            "source_id": locator.get("source_id"),
            "kind": locator.get("kind"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _digest(prefix, basis)
