import hashlib
import os
import re
from datetime import datetime, timezone

from vector_lake.wiki_utils import canonical_source_name, normalize_sources
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


# Reader-facing markers written by the page-shaping tools (``tool_query``, the
# lint stub builder) and by the page template itself.  They are emitted *inside*
# the compiled-truth and timeline sections, so the heading rule below turns them
# into claims even though they record nothing.
SYSTEM_DIRECTIVE_MARKER = "[System Directive:"
# The template's section caption, written as a paragraph above the ledger.  On the live
# corpus 199 claims are exactly ``(Timeline - EVENT STORE)``: no date, no proposition.
TIMELINE_CAPTION_PREFIXES = ("(timeline", "timeline - event store")
# The scope disclaimer the ingest template puts above the ledger.  On the live corpus 345
# claims carry that same sentence.
PAGE_SCOPE_MARKER = "本页只记录证据"

# The heading markers that decide a block's claim type.  They live here, next to the rule that
# uses them, because a bare ``证据`` used to qualify a block as a ledger entry -- which typed
# every evidence-boundary/limitation section as an event log: on the live corpus 1595 claims
# came from headings like ``证据边界`` and 99% of them carried no date.
COMPILED_TRUTH_HEADING_MARKERS = ("编译事实", "compiled truth", "事实", "truth")
LEDGER_HEADING_MARKERS = ("证据时间线", "时间线", "timeline", "event store")


def claim_type_for_heading(kind: str, heading: str, text: str) -> str:
    """The claim type the heading rule gives one block of a page.

    Named rather than inlined in :func:`extract_page_objects` because the classification is
    also re-derived against already-stored claims (``scripts/reclassify_timeline_claims.py``)
    when the rule changes; two copies of it would drift.
    """
    heading_lower = (heading or "").lower()
    if any(k in heading_lower for k in COMPILED_TRUTH_HEADING_MARKERS):
        return "compiled-truth"
    if (
        any(k in heading_lower for k in LEDGER_HEADING_MARKERS)
        and not _is_page_scope_disclaimer(text)
    ):
        return "timeline-event"
    return _claim_type_for_block(kind)


def _is_page_boilerplate(text) -> bool:
    """True for a block that only carries a template caption or a reader instruction.

    Neither is a claim, but both sit under a ``证据时间线``/``编译事实`` heading.  A
    directive's text is rewritten on every reshape (it embeds a ``Last Reshaped`` date),
    and because the timeline projection id is content-addressed, each reshape moved the id
    and orphaned the previous ``timeline_events`` row -- the one defect that kept
    regenerating timeline-projection drift.  The caption is markup: no date and no
    proposition, yet it reached the timeline as an undated "event" that the projection then
    dated with its *ingestion* time.
    """
    normalized = _collapse_text(text).lower()
    if not normalized:
        return False
    if SYSTEM_DIRECTIVE_MARKER.lower() in normalized:
        return True
    return any(normalized.startswith(prefix) for prefix in TIMELINE_CAPTION_PREFIXES)


def _is_page_scope_disclaimer(text) -> bool:
    """The scope sentence the ingest template puts above the ledger.

    It says how the page was built, so it stays a claim -- it is just not a ledger entry.
    Reaching the timeline it became an undated "event" on 345 live rows, each dated by the
    projection with its own ingestion time.
    """
    return _collapse_text(text).lower().startswith(PAGE_SCOPE_MARKER)


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
        """The date a ledger entry carries as a ``[...]`` prefix, or ``None``.

        The prefix is deliberately *not* stripped.  This text is the input to ``claim_id``,
        ``evidence_id`` and the embedded ``claim_text``, so removing it would re-mint the
        identity of every dated entry on the next compile, while the projection reads the
        same date off the text either way (``tool_timeline._TEXT_EVENT_DATE``).  It would be
        a corpus-wide identity rewrite for nothing.

        The previous pattern accepted only ``[2026-07]`` / ``[2026-Q1]`` (its ``[H|Q]`` is a
        character class, not an alternation) and silently skipped the ``[YYYY-MM-DD]`` form
        the ledger format in ``README`` actually mandates -- which is why 7246 of 9974 live
        timeline claims carried no ``temporal_anchor`` and every reader had to re-parse the
        date out of free text.
        """
        match = re.match(
            r"^\s*\[(\d{4}-\d{2}-\d{2}|\d{4}-[QH]\d|\d{4}-\d{2}|\d{4})\]",
            str(text or ""),
        )
        return match.group(1) if match else None

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

    clean_body = re.sub(r"```.*?```", "", body, flags=re.DOTALL)
    clean_body = re.sub(r"`.*?`", "", clean_body)
    triples = []
    links = set()
    page_edges = []
    for match in re.finditer(r"\[([^\[\]]+?)::\s*\[\[(.*?)\]\]\]", clean_body):
        predicate = match.group(1).strip()
        target = match.group(2).split("|")[0].strip().replace(".md", "")
        if target:
            links.add(target)
            triples.append({"predicate": predicate, "target": target})
            page_edges.append({
                "source_id": page_key,
                "target_id": target,
                "relation": predicate,
                "weight": 1.0,
                "updated_at": now,
            })

    entity_id = frontmatter.get("entity_id") or _stable_id("entity", page_key)
    subject_entity_ids.append(entity_id)
    entity_records.append({
        "entity_id": entity_id,
        "id": frontmatter.get("id") or entity_id,
        "page_key": page_key,
        "canonical_name": title,
        "title": title,
        "type": page_type,
        "entity_type": page_type,
        "status": frontmatter.get("status", "Active"),
        "epistemic-status": frontmatter.get("epistemic-status", "draft"),
        "aliases": aliases,
        "domain": frontmatter.get("domain", "General"),
        "topic_cluster": frontmatter.get("topic_cluster", "General"),
        "categories": _jsonable(frontmatter.get("categories", [])),
        "tags": _jsonable(frontmatter.get("tags", [])),
        "sources": sources,
        "tension_edges": _jsonable(frontmatter.get("tension_edges", [])),
        "relations": _jsonable(frontmatter.get("relations", [])),
        "links": sorted(links),
        "outbound_links": sorted(links),
        "triples": triples,
        "summary": summary,
        "raw_text": body,
        "ttl": frontmatter.get("ttl"),
        "created_at": _jsonable(frontmatter.get("created", now)),
        "updated": _jsonable(frontmatter.get("updated", now)),
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
            "canonical_source_page": (page_name if page_type == "source"
                else canonical_source_name(raw_ref)),
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
        if _is_page_boilerplate(block["text"]) or _is_page_boilerplate(block.get("raw_text")):
            continue
        block_temporal = _parse_temporal(block["text"])
        final_temporal = block_temporal or validity_defaults.get("temporal_anchor")
        # ``_iter_blocks`` already cleaned this; ``_parse_temporal`` adds an anchor without
        # editing it, so the claim text is the block text verbatim.
        block_text = block["text"]

        raw_text = block.get("raw_text", block["text"])
        inline_sources = _parse_inline_sources(raw_text)

        custom_claim_type = claim_type_for_heading(
            block["kind"], block.get("heading") or title, block_text
        )
            
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
                        "canonical_source_page": canonical_source_name(isrc),
                        "source_type": os.path.splitext(isrc)[1].lstrip(".").lower() or "md",
                        "title": os.path.basename(isrc),
                        "ingested_at": now,
                        "content_hash": _stable_id("hash", isrc + page_name),
                    })

        evidence_ids = []
        for raw_ref, source_id in zip(combined_sources, combined_source_ids):
            if len(sources) > 1 and page_type != "source" and raw_ref not in inline_sources:
                continue
            evidence_id = _stable_id("evidence", f"{page_key}:{raw_ref}:{block_text}")
            evidence_ids.append(evidence_id)
            evidence_records.append({
                "evidence_id": evidence_id,
                "source_id": source_id,
                "locator": {
                    "page_key": page_key,
                    "heading": block.get("heading") or title,
                    "block_index": block_index,
                },
                "evidence_text": block_text,
                "evidence_type": f"block-{block['kind']}",
                "created_at": now,
                "supports_claim_ids": [],
                "contradicts_claim_ids": [],
            })

        claim_id = frontmatter.get("claim_id") if block_index == 1 else None
        claim_id = claim_id or _stable_id("claim", f"{page_key}:{block_text}")
        from vector_lake.wiki_utils import enforce_claim_dict
        claim_record = enforce_claim_dict({
            "claim_id": claim_id,
            "claim_text": block_text,
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
        summary_temporal = _parse_temporal(summary)
        final_summary_temporal = summary_temporal or validity_defaults.get("temporal_anchor")

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
        summary_claim = enforce_claim_dict({
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
            "temporal_anchor": final_summary_temporal,
            "created_at": _jsonable(frontmatter.get("created", now)),
            "updated_at": _jsonable(frontmatter.get("updated", now)),
            "source_page": page_name,
        })
        if summary_evidence_ids:
            claim_records.append(summary_claim)
            for evidence_record in evidence_records[-len(summary_evidence_ids):]:
                evidence_record["supports_claim_ids"].append(summary_claim_id)

    return {
        "entities": entity_records,
        "claims": claim_records,
        "evidence": evidence_records,
        "sources": source_records,
        "edges": page_edges,
        "page_key": page_key,
        "page_type": page_type,
    }

