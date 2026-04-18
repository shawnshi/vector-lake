import hashlib
import os
import re
from datetime import datetime, timezone

from vector_lake.wiki_utils import normalize_sources


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.md5(value.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


def _jsonable(value):
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            return str(value)
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _collapse_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _body_summary(body: str, limit: int = 320) -> str:
    return _collapse_text(body)[:limit]


def _heading_to_text(line: str) -> str:
    return re.sub(r"^#+\s*", "", line).strip()


def _clean_claim_text(text: str, limit: int = 360) -> str:
    cleaned = _collapse_text(text)
    cleaned = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", cleaned)
    cleaned = re.sub(r"\[\[([^\]]+)\]\]", r"\1", cleaned)
    cleaned = re.sub(r"\[([^\[\]]+?)::\s*\[\[(.*?)\]\]\]", r"\1 \2", cleaned)
    return cleaned[:limit]


def _iter_blocks(body: str) -> list[dict]:
    blocks = []
    current_heading = None
    paragraph_lines = []

    def flush_paragraph():
        nonlocal paragraph_lines
        if not paragraph_lines:
            return
        text = _clean_claim_text(" ".join(paragraph_lines))
        if text:
            blocks.append({
                "kind": "paragraph",
                "heading": current_heading,
                "text": text,
            })
        paragraph_lines = []

    for raw_line in (body or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            continue
        if stripped.startswith("#"):
            flush_paragraph()
            current_heading = _heading_to_text(stripped)
            continue
        if re.match(r"^[-*+]\s+", stripped):
            flush_paragraph()
            text = _clean_claim_text(re.sub(r"^[-*+]\s+", "", stripped))
            if text:
                blocks.append({
                    "kind": "bullet",
                    "heading": current_heading,
                    "text": text,
                })
            continue
        paragraph_lines.append(stripped)

    flush_paragraph()
    return blocks


def _claim_type_for_block(kind: str) -> str:
    if kind == "bullet":
        return "bullet-claim"
    return "assertion"


def _validity_defaults(frontmatter: dict) -> dict:
    return {
        "valid_from": _jsonable(frontmatter.get("valid_from")),
        "valid_to": _jsonable(frontmatter.get("valid_to")),
        "review_after": _jsonable(frontmatter.get("review_after")),
        "freshness_tier": frontmatter.get("freshness_tier", "unknown"),
    }


def extract_page_objects(page_path: str, frontmatter: dict, body: str) -> dict:
    now = _utc_now()
    page_name = os.path.basename(page_path)
    page_key = os.path.splitext(page_name)[0]
    title = frontmatter.get("title", page_key)
    page_type = str(frontmatter.get("type", "concept")).lower()
    aliases = frontmatter.get("aliases", [])
    if isinstance(aliases, str):
        aliases = [aliases]
    aliases = [str(alias).strip() for alias in aliases if str(alias).strip()]
    sources = normalize_sources(frontmatter.get("sources", []))
    summary = frontmatter.get("summary") or _body_summary(body)
    validity_defaults = _validity_defaults(frontmatter)

    subject_entity_ids = []
    entity_records = []
    source_records = []
    evidence_records = []
    claim_records = []

    if page_type != "source":
        entity_id = frontmatter.get("entity_id") or _stable_id("entity", page_key)
        subject_entity_ids.append(entity_id)
        entity_records.append({
            "entity_id": entity_id,
            "canonical_name": title,
            "entity_type": page_type,
            "status": frontmatter.get("status", "Active"),
            "aliases": aliases,
            "domain": frontmatter.get("domain", "General"),
            "topic_cluster": frontmatter.get("topic_cluster", "General"),
            "tags": _jsonable(frontmatter.get("tags", [])),
            "page_key": page_key,
            "created_at": _jsonable(frontmatter.get("created", now)),
            "updated_at": _jsonable(frontmatter.get("updated", now)),
            "source_page": page_name,
        })

    source_ids = []
    for raw_ref in sources:
        source_id = _stable_id("source", raw_ref)
        source_ids.append(source_id)
        source_records.append({
            "source_id": source_id,
            "raw_ref": raw_ref,
            "canonical_source_page": page_name if page_type == "source" else f"Source_{os.path.splitext(os.path.basename(raw_ref))[0]}.md",
            "source_type": os.path.splitext(raw_ref)[1].lstrip(".").lower() or "md",
            "title": title if page_type == "source" else os.path.basename(raw_ref),
            "ingested_at": now,
            "content_hash": frontmatter.get("id") or _stable_id("hash", raw_ref + page_name),
        })

    blocks = _iter_blocks(body)
    if not blocks and summary:
        blocks = [{
            "kind": "paragraph",
            "heading": title,
            "text": summary,
        }]

    for block_index, block in enumerate(blocks, start=1):
        evidence_ids = []
        for raw_ref, source_id in zip(sources, source_ids):
            evidence_id = _stable_id("evidence", f"{page_key}:{block_index}:{raw_ref}:{block['text']}")
            evidence_ids.append(evidence_id)
            evidence_records.append({
                "evidence_id": evidence_id,
                "source_id": source_id,
                "locator": {
                    "page_key": page_key,
                    "heading": block.get("heading") or title,
                    "block_index": block_index,
                },
                "evidence_text": block["text"],
                "evidence_type": f"block-{block['kind']}",
                "created_at": now,
                "supports_claim_ids": [],
                "contradicts_claim_ids": [],
            })

        claim_id = frontmatter.get("claim_id") if block_index == 1 else None
        claim_id = claim_id or _stable_id("claim", f"{page_key}:{block_index}:{block['text']}")
        claim_record = {
            "claim_id": claim_id,
            "claim_text": block["text"],
            "claim_type": _claim_type_for_block(block["kind"]),
            "claim_scope": "block",
            "status": frontmatter.get("status", "Active"),
            "confidence": frontmatter.get("confidence", 0.6 if page_type == "synthesis" else 0.8),
            "subject_entity_ids": list(subject_entity_ids),
            "evidence_ids": evidence_ids,
            "source_ids": list(source_ids),
            "locator": {
                "page_key": page_key,
                "heading": block.get("heading") or title,
                "block_index": block_index,
            },
            **validity_defaults,
            "created_at": _jsonable(frontmatter.get("created", now)),
            "updated_at": _jsonable(frontmatter.get("updated", now)),
            "source_page": page_name,
        }
        claim_records.append(claim_record)
        if evidence_ids:
            for evidence_record in evidence_records[-len(evidence_ids):]:
                evidence_record["supports_claim_ids"].append(claim_id)

    if summary:
        summary_evidence_ids = []
        for raw_ref, source_id in zip(sources, source_ids):
            evidence_id = _stable_id("evidence", f"{page_key}:summary:{raw_ref}")
            summary_evidence_ids.append(evidence_id)
            evidence_records.append({
                "evidence_id": evidence_id,
                "source_id": source_id,
                "locator": {"page_key": page_key, "heading": title, "block_index": 0},
                "evidence_text": summary,
                "evidence_type": "page-summary",
                "created_at": now,
                "supports_claim_ids": [],
                "contradicts_claim_ids": [],
            })

        summary_claim_id = _stable_id("claim", f"{page_key}:summary:{summary}")
        summary_claim = {
            "claim_id": summary_claim_id,
            "claim_text": summary,
            "claim_type": "summary",
            "claim_scope": "page",
            "status": frontmatter.get("status", "Active"),
            "confidence": frontmatter.get("confidence", 0.65 if page_type == "synthesis" else 0.82),
            "subject_entity_ids": list(subject_entity_ids),
            "evidence_ids": summary_evidence_ids,
            "source_ids": list(source_ids),
            "locator": {"page_key": page_key, "heading": title, "block_index": 0},
            **validity_defaults,
            "created_at": _jsonable(frontmatter.get("created", now)),
            "updated_at": _jsonable(frontmatter.get("updated", now)),
            "source_page": page_name,
        }
        claim_records.append(summary_claim)
        if summary_evidence_ids:
            for evidence_record in evidence_records[-len(summary_evidence_ids):]:
                evidence_record["supports_claim_ids"].append(summary_claim_id)

    return {
        "entities": entity_records,
        "claims": claim_records,
        "evidence": evidence_records,
        "sources": source_records,
        "page_key": page_key,
        "page_type": page_type,
    }

