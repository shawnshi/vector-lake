from pathlib import Path

import governance_metrics
import governance_store
from wiki_utils import atomic_write_text, get_wiki_dir


def get_views_dir() -> Path:
    path = get_wiki_dir() / "views"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _entity_view_path(entity: dict) -> Path:
    page_key = entity.get("page_key") or entity.get("entity_id")
    return get_views_dir() / f"{page_key}.md"


def build_entity_view(entity_id: str) -> str:
    entities = governance_store.load_entities()["items"]
    claims = governance_store.load_claims()["items"].values()
    sources = governance_store.load_sources()["items"]
    entity = entities.get(entity_id)
    if not entity:
        return ""

    related_claims = [
        governance_metrics.annotate_claim_validity(claim)
        for claim in claims
        if entity_id in claim.get("subject_entity_ids", [])
    ]
    related_claims.sort(key=lambda claim: (claim.get("validity_state") != "active", -float(claim.get("confidence", 0) or 0)))

    lines = [
        f"# {entity['canonical_name']}",
        "",
        "<!-- generated: canonical governance view -->",
        "",
        "## Profile",
        "",
        f"- Entity ID: `{entity['entity_id']}`",
        f"- Type: `{entity.get('entity_type', 'unknown')}`",
        f"- Domain: `{entity.get('domain', 'General')}`",
        f"- Topic Cluster: `{entity.get('topic_cluster', 'General')}`",
        f"- Status: `{entity.get('status', 'Active')}`",
    ]
    aliases = entity.get("aliases", [])
    if aliases:
        lines.append(f"- Aliases: {', '.join(aliases)}")

    lines.extend(["", "## Claims", ""])
    if not related_claims:
        lines.append("- No published claims.")
    else:
        for claim in related_claims[:20]:
            lines.append(f"### {claim.get('claim_type', 'claim').title()} | `{claim.get('validity_state', 'unknown')}`")
            lines.append("")
            lines.append(claim.get("claim_text", ""))
            lines.append("")
            lines.append(f"- Claim ID: `{claim['claim_id']}`")
            lines.append(f"- Confidence: `{claim.get('confidence')}`")
            locator = claim.get("locator", {})
            if locator:
                lines.append(f"- Locator: `{locator.get('page_key', '')}#{locator.get('heading', '')}:{locator.get('block_index', '')}`")
            if claim.get("review_after"):
                lines.append(f"- Review After: `{claim['review_after']}`")
            if claim.get("valid_to"):
                lines.append(f"- Valid To: `{claim['valid_to']}`")
            source_pages = [
                sources[source_id]["canonical_source_page"]
                for source_id in claim.get("source_ids", [])
                if source_id in sources
            ]
            if source_pages:
                lines.append(f"- Sources: {', '.join(source_pages[:5])}")
            lines.append("")

    return "\n".join(lines).strip() + "\n"


def rebuild_entity_view(entity_id: str) -> str | None:
    entities = governance_store.load_entities()["items"]
    entity = entities.get(entity_id)
    if not entity:
        return None
    output_path = _entity_view_path(entity)
    atomic_write_text(output_path, build_entity_view(entity_id))
    return str(output_path)


def build_open_questions_view(limit: int = 25) -> str:
    claims = [
        governance_metrics.annotate_claim_validity(claim)
        for claim in governance_store.load_claims()["items"].values()
    ]
    debt_claims = [
        claim for claim in claims
        if claim.get("validity_state") in {"unsupported", "review-due", "conflicted", "provisional", "expired"}
    ]
    debt_claims.sort(key=lambda claim: (claim.get("validity_state"), claim.get("source_page", "")))

    lines = [
        "# Open Questions",
        "",
        "<!-- generated: governance debt view -->",
        "",
    ]
    if not debt_claims:
        lines.append("- No high-priority governance debt.")
    else:
        for claim in debt_claims[:limit]:
            lines.append(f"- `{claim.get('validity_state', 'unknown')}` {claim.get('claim_text', '')} ({claim.get('source_page', '')})")
    lines.append("")
    return "\n".join(lines)


def rebuild_open_questions_view() -> str:
    output_path = get_views_dir() / "open_questions.md"
    atomic_write_text(output_path, build_open_questions_view())
    return str(output_path)


def rebuild_views_for_change_set(change_set: dict) -> dict:
    entity_ids = []
    for entity in change_set.get("proposed_entities", []):
        entity_id = entity.get("entity_id")
        if entity_id:
            entity_ids.append(entity_id)

    built_paths = []
    for entity_id in sorted(set(entity_ids)):
        built = rebuild_entity_view(entity_id)
        if built:
            built_paths.append(built)

    built_paths.append(rebuild_open_questions_view())
    return {
        "rebuilt": len(built_paths),
        "paths": built_paths,
    }
