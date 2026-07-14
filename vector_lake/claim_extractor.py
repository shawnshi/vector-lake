import hashlib
import os
import re
from datetime import datetime, timezone

from vector_lake.wiki_utils import normalize_sources
from vector_lake.schema_validator import validate_schema, SchemaViolationException
import logging

log = logging.getLogger("vector-lake-claim-extractor")

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=12).hexdigest()
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
    # Strip inline sources completely to reduce RAG noise
    cleaned = re.sub(r"\(Source[s]?:\s*(.*?)\)", "", cleaned, flags=re.IGNORECASE)
    # Strip typed links first: [predicate:: [[Target|Alias]]] -> Alias or [predicate:: [[Target]]] -> Target
    cleaned = re.sub(r"\[([^\[\]]+?)::\s*\[\[([^\]|]+)\|([^\]]+)\]\]\]", r"\3", cleaned)
    cleaned = re.sub(r"\[([^\[\]]+?)::\s*\[\[(.*?)\]\]\]", r"\2", cleaned)
    # Then strip legacy links
    cleaned = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", cleaned)
    cleaned = re.sub(r"\[\[([^\]]+)\]\]", r"\1", cleaned)
    return cleaned[:limit]


def _iter_blocks(body: str) -> list[dict]:
    import mistune
    markdown = mistune.create_markdown(renderer='ast')
    ast = markdown(body or "")
    
    blocks = []
    current_heading = None

    def extract_text(node) -> str:
        if isinstance(node, dict):
            if node.get("type") == "block_code":
                return " "
            if node.get("type") in ("softbreak", "hardbreak"):
                return " "
            text = node.get("raw", "")
            for child in node.get("children", []):
                text += extract_text(child)
            return text
        return ""

    def process_node(node):
        nonlocal current_heading
        if node["type"] == "heading":
            current_heading = extract_text(node).strip()
        elif node["type"] == "paragraph":
            raw_text = extract_text(node).strip()
            text = _clean_claim_text(raw_text)
            if text:
                blocks.append({
                    "kind": "paragraph",
                    "heading": current_heading,
                    "text": text,
                    "raw_text": raw_text,
                })
        elif node["type"] == "list":
            for child in node.get("children", []):
                if child["type"] == "list_item":
                    raw_text = extract_text(child).strip()
                    text = _clean_claim_text(raw_text)
                    if text:
                        blocks.append({
                            "kind": "bullet",
                            "heading": current_heading,
                            "text": text,
                            "raw_text": raw_text,
                        })

    for node in ast:
        process_node(node)

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
        "temporal_anchor": frontmatter.get("temporal_anchor"),
        "memory_type": frontmatter.get("memory_type"),
        "memory_key": frontmatter.get("memory_key"),
        "authority_score": frontmatter.get("authority_score"),
        "importance_score": frontmatter.get("importance_score"),
        "reinforcement_count": frontmatter.get("reinforcement_count"),
        "ttl_days": frontmatter.get("ttl_days") or frontmatter.get("ttl"),
    }


def extract_page_objects(page_path: str, frontmatter: dict, body: str) -> dict:
    now = _utc_now()
    page_name = os.path.basename(page_path)
    page_key = os.path.splitext(page_name)[0]
    title = frontmatter.get("title", page_key)
    page_type = str(frontmatter.get("type", "concept")).lower()
    aliases = frontmatter.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    aliases = [str(alias).strip() for alias in aliases if alias and str(alias).strip()]
    sources = normalize_sources(frontmatter.get("sources") or [])
    summary = frontmatter.get("summary") or _body_summary(body)
    validity_defaults = _validity_defaults(frontmatter)

    try:
        validate_schema(frontmatter, body, page_name)
    except SchemaViolationException as e:
        log.warning(f"Validation failed for {page_name}, skipping extraction: {e}")
        return {
            "entities": [],
            "claims": [],
            "evidence": [],
            "sources": [],
            "edges": [],
            "page_key": page_key,
            "page_type": page_type,
        }

    def _parse_temporal(text: str):
        match = re.match(r"^\[(20\d\d(?:-[H|Q]\d|-[0-1]\d)?)\]\s*", text)
        if match:
            return match.group(1), text[match.end():]
        return None, text

    def _parse_inline_sources(raw_text: str):
        found_sources = []
        for match in re.finditer(r"\(Source[s]?:\s*(.*?)\)", raw_text, flags=re.IGNORECASE):
            content = match.group(1)
            for m2 in re.finditer(r"\[\[(.*?)\]\]", content):
                found_sources.append(m2.group(1).split("|")[0].strip().replace(".md", ""))
        return found_sources

    subject_entity_ids = []
    entity_records = []
    source_records = []
    evidence_records = []
    claim_records = []

    if page_type != "source":
        entity_id = frontmatter.get("entity_id") or _stable_id("entity", page_key)
        subject_entity_ids.append(entity_id)
        raw_tension_edges = frontmatter.get("tension_edges", [])
        tension_edges = []
        if isinstance(raw_tension_edges, list):
            for te in raw_tension_edges:
                if isinstance(te, dict) and te.get("target"):
                    tension_edges.append({
                        "target": str(te.get("target")).strip(),
                        "polarity": float(te.get("polarity", 0.0)),
                        "intensity": float(te.get("intensity", 0.0)),
                        "context": str(te.get("context", "")).strip()
                    })

        entity_records.append({
            "entity_id": entity_id,
            "canonical_name": title,
            "entity_type": page_type,
            "status": frontmatter.get("status", "Active"),
            "aliases": aliases,
            "domain": frontmatter.get("domain", "General"),
            "topic_cluster": frontmatter.get("topic_cluster", "General"),
            "tags": _jsonable(frontmatter.get("tags", [])),
            "tension_edges": tension_edges,
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
        block_temporal, cleaned_text = _parse_temporal(block["text"])
        final_temporal = block_temporal or validity_defaults.get("temporal_anchor")

        raw_text = block.get("raw_text", block["text"])
        inline_sources = _parse_inline_sources(raw_text)

        custom_claim_type = _claim_type_for_block(block["kind"])
        heading = block.get("heading") or title
        heading_lower = (heading or "").lower()
        if any(k in heading_lower for k in ["编译事实", "compiled truth", "事实", "truth"]):
            custom_claim_type = "compiled-truth"
        elif any(k in heading_lower for k in ["证据时间线", "timeline", "证据", "时间线"]):
            custom_claim_type = "timeline-event"
            
        combined_sources = list(sources)
        combined_source_ids = list(source_ids)
        for isrc in inline_sources:
            if isrc not in combined_sources:
                combined_sources.append(isrc)
                sid = _stable_id("source", isrc)
                combined_source_ids.append(sid)
                if not any(s["source_id"] == sid for s in source_records):
                    source_records.append({
                        "source_id": sid,
                        "raw_ref": isrc,
                        "canonical_source_page": f"Source_{os.path.splitext(os.path.basename(isrc))[0]}.md",
                        "source_type": os.path.splitext(isrc)[1].lstrip(".").lower() or "md",
                        "title": os.path.basename(isrc),
                        "ingested_at": now,
                        "content_hash": _stable_id("hash", isrc + page_name),
                    })

        evidence_ids = []
        for raw_ref, source_id in zip(combined_sources, combined_source_ids):
            if len(sources) > 1 and page_type != "source" and raw_ref not in inline_sources:
                continue
            evidence_id = _stable_id("evidence", f"{page_key}:{raw_ref}:{cleaned_text}")
            evidence_ids.append(evidence_id)
            evidence_records.append({
                "evidence_id": evidence_id,
                "source_id": source_id,
                "locator": {
                    "page_key": page_key,
                    "heading": block.get("heading") or title,
                    "block_index": block_index,
                },
                "evidence_text": cleaned_text,
                "evidence_type": f"block-{block['kind']}",
                "created_at": now,
                "supports_claim_ids": [],
                "contradicts_claim_ids": [],
            })

        claim_id = frontmatter.get("claim_id") if block_index == 1 else None
        claim_id = claim_id or _stable_id("claim", f"{page_key}:{cleaned_text}")
        from vector_lake.wiki_utils import enforce_claim_dict
        claim_record = enforce_claim_dict({
            "claim_id": claim_id,
            "claim_text": cleaned_text,
            "claim_type": custom_claim_type,
            "claim_scope": "block",
            "status": frontmatter.get("status", "Active"),
            "confidence": frontmatter.get("confidence", 0.6 if page_type == "synthesis" else 0.8),
            "subject_entity_ids": list(subject_entity_ids),
            "evidence_ids": evidence_ids,
            "source_ids": list(source_ids),
            "inline_sources": inline_sources,
            "locator": {
                "page_key": page_key,
                "heading": block.get("heading") or title,
                "block_index": block_index,
            },
            **validity_defaults,
            "temporal_anchor": final_temporal,
            "created_at": _jsonable(frontmatter.get("created", now)),
            "updated_at": _jsonable(frontmatter.get("updated", now)),
            "source_page": page_name,
        })
        claim_records.append(claim_record)
        if evidence_ids:
            for evidence_record in evidence_records[-len(evidence_ids):]:
                evidence_record["supports_claim_ids"].append(claim_id)

    if summary:
        summary_temporal, cleaned_summary = _parse_temporal(summary)
        final_summary_temporal = summary_temporal or validity_defaults.get("temporal_anchor")

        summary_evidence_ids = []
        for raw_ref, source_id in zip(sources, source_ids):
            evidence_id = _stable_id("evidence", f"{page_key}:summary:{raw_ref}")
            summary_evidence_ids.append(evidence_id)
            evidence_records.append({
                "evidence_id": evidence_id,
                "source_id": source_id,
                "locator": {"page_key": page_key, "heading": title, "block_index": 0},
                "evidence_text": cleaned_summary,
                "evidence_type": "page-summary",
                "created_at": now,
                "supports_claim_ids": [],
                "contradicts_claim_ids": [],
            })

        summary_claim_id = _stable_id("claim", f"{page_key}:summary:{cleaned_summary}")
        summary_claim = enforce_claim_dict({
            "claim_id": summary_claim_id,
            "claim_text": cleaned_summary,
            "claim_type": "summary",
            "claim_scope": "page",
            "status": frontmatter.get("status", "Active"),
            "confidence": frontmatter.get("confidence", 0.65 if page_type == "synthesis" else 0.82),
            "subject_entity_ids": list(subject_entity_ids),
            "evidence_ids": summary_evidence_ids,
            "source_ids": list(source_ids),
            "locator": {"page_key": page_key, "heading": title, "block_index": 0},
            **validity_defaults,
            "temporal_anchor": final_summary_temporal,
            "created_at": _jsonable(frontmatter.get("created", now)),
            "updated_at": _jsonable(frontmatter.get("updated", now)),
            "source_page": page_name,
        })
        if summary_evidence_ids:
            claim_records.append(summary_claim)
            for evidence_record in evidence_records[-len(summary_evidence_ids):]:
                evidence_record["supports_claim_ids"].append(summary_claim_id)

    page_edges = []
    for match in re.finditer(r"\[([^\[\]]+?)::\s*\[\[(.*?)\]\]\]", body):
        predicate = match.group(1).strip()
        target = match.group(2).split("|")[0].strip().replace(".md", "")
        if target:
            page_edges.append({"source_id": page_key, "target_id": target, "relation": predicate, "weight": 1.0, "updated_at": now})

    return {
        "entities": entity_records,
        "claims": claim_records,
        "evidence": evidence_records,
        "sources": source_records,
        "edges": page_edges,
        "page_key": page_key,
        "page_type": page_type,
    }

