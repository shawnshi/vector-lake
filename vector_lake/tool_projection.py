"""Projection/canonical maintenance helpers.

These tools are intentionally conservative: report first, then bounded apply
with a live backup. They repair recoverable projections without deleting
canonical history.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from vector_lake import indexer
from vector_lake.claim_extractor import extract_page_objects
from vector_lake.db_store import backup_database, get_connection, get_db_path, init_db
from vector_lake.node_vocabulary import NON_NODE_WIKI_FILES
from vector_lake.schema_validator import VALID_H3_SLOTS
from vector_lake.yaml_utils import dump_yaml
from vector_lake.wiki_utils import get_claim_graph_path, get_index_path, get_meta_dir, get_wiki_dir, read_markdown_file


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _wiki_keys() -> set[str]:
    wiki_dir = get_wiki_dir()
    if not wiki_dir.exists():
        return set()
    return {
        path.stem
        for path in wiki_dir.glob("*.md")
        if path.is_file() and path.name not in NON_NODE_WIKI_FILES and not path.name.startswith("System_")
    }


def _canonical_keys() -> set[str]:
    # Unconditionally, for the same reason the health gate does it: the query below names a column
    # that only exists once the DDL (or its ALTER) has run.
    init_db()
    conn = get_connection()
    return {
        row["page_key"]
        for row in conn.execute(
            "SELECT f_page_key AS page_key FROM entities "
            "WHERE f_page_key IS NOT NULL"
        )
        if row["page_key"] and not str(row["page_key"]).startswith("System_")
    }


def _index_keys() -> set[str]:
    index_path = get_index_path()
    if not index_path.exists():
        return set()
    data = json.loads(index_path.read_text(encoding="utf-8"))
    return {key for key in data.get("nodes", {}) if not str(key).startswith("System_")}


def _diff_sets() -> dict[str, set[str]]:
    wiki = _wiki_keys()
    canonical = _canonical_keys()
    index = _index_keys()
    return {
        "wiki": wiki,
        "canonical": canonical,
        "index": index,
        "missing_index": canonical - index,
        "extra_index": index - canonical,
        "missing_canonical": wiki - canonical,
        "extra_canonical": canonical - wiki,
    }


def create_maintenance_backup(label: str = "maintenance") -> str:
    """Create a consistent SQLite backup and copy recoverable projections."""
    backup_dir = get_meta_dir() / "backups" / f"{label}_{_utc_stamp()}"
    # Same-second invocations must not collide; a failed mkdir used to abort the repair.
    backup_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    if get_db_path().exists():
        target = backup_dir / get_db_path().name
        backup_database(target)
        copied.append(target.name)
    for path in [get_index_path(), get_claim_graph_path()]:
        if Path(path).exists():
            target = backup_dir / Path(path).name
            shutil.copy2(path, target)
            copied.append(target.name)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "label": label,
        "copied": copied,
    }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(backup_dir)


def projection_diff_report(limit: int = 20) -> str:
    diff = _diff_sets()
    limit = max(0, int(limit))

    lines = [
        "=== Projection / Canonical Diff ===",
        f"Wiki pages: {len(diff['wiki'])}",
        f"SQLite canonical entities: {len(diff['canonical'])}",
        f"index.json nodes: {len(diff['index'])}",
        f"missing_index: {len(diff['missing_index'])}",
        f"extra_index: {len(diff['extra_index'])}",
        f"missing_canonical: {len(diff['missing_canonical'])}",
        f"extra_canonical: {len(diff['extra_canonical'])}",
    ]
    for key in ("missing_index", "extra_index", "missing_canonical", "extra_canonical"):
        sample = sorted(diff[key])[:limit]
        if sample:
            lines.append(f"{key} sample: {', '.join(sample)}")
    return "\n".join(lines)


def _preview_backfill_pages(page_keys: list[str]) -> dict:
    wiki_dir = get_wiki_dir()
    valid = 0
    invalid: list[str] = []
    proposed_entities = 0
    proposed_claims = 0
    for page_key in page_keys:
        path = wiki_dir / f"{page_key}.md"
        try:
            frontmatter, body, _ = read_markdown_file(path)
            extracted = extract_page_objects(str(path), frontmatter, body)
        except Exception as exc:
            invalid.append(f"{page_key}: {exc}")
            continue
        entities = extracted.get("entities", [])
        if not entities:
            invalid.append(page_key)
            continue
        valid += 1
        proposed_entities += len(entities)
        proposed_claims += len(extracted.get("claims", []))
    return {
        "valid_pages": valid,
        "invalid_pages": invalid,
        "proposed_entities": proposed_entities,
        "proposed_claims": proposed_claims,
    }


def canonical_backfill_missing_wiki(dry_run: bool = True, limit: int = 50) -> str:
    """Backfill SQLite canonical rows from Wiki pages missing in canonical."""
    diff = _diff_sets()
    page_keys = sorted(diff["missing_canonical"])[: max(1, int(limit))]
    preview = _preview_backfill_pages(page_keys)
    if dry_run:
        invalid_sample = preview["invalid_pages"][:10]
        lines = [
            f"[DRY RUN] Would inspect {len(page_keys)} of {len(diff['missing_canonical'])} missing-canonical page(s).",
            f"valid_pages: {preview['valid_pages']}",
            f"invalid_pages: {len(preview['invalid_pages'])}",
            f"proposed_entities: {preview['proposed_entities']}",
            f"proposed_claims: {preview['proposed_claims']}",
        ]
        if invalid_sample:
            lines.append(f"invalid sample: {', '.join(invalid_sample)}")
        return "\n".join(lines)

    if not page_keys:
        return "No missing-canonical wiki pages to backfill."

    backup_dir = create_maintenance_backup("canonical_backfill")
    from vector_lake.mutation_coordinator import execute_mutation_batch

    mutations = []
    for page_key in page_keys:
        path = get_wiki_dir() / f"{page_key}.md"
        mutations.append({"filename": path.name, "content": path.read_text(encoding="utf-8")})
    _, detail = execute_mutation_batch(mutations, validation_mode="schema")
    return (
        f"Backfilled {len(mutations)} wiki page(s) into canonical store. "
        f"{detail}; backup={backup_dir}"
    )


def rebuild_index_projection(dry_run: bool = True) -> str:
    """Rebuild index.json / FTS / claim_topology from SQLite canonical state."""
    diff = _diff_sets()
    if dry_run:
        return (
            "[DRY RUN] Would rebuild index.json, wiki_search_index, and claim_topology.json "
            f"from {len(diff['canonical'])} canonical entity row(s), while preserving existing vec_embeddings. "
            f"Current drift: missing_index={len(diff['missing_index'])}, extra_index={len(diff['extra_index'])}."
        )

    backup_dir = create_maintenance_backup("index_rebuild")
    output = indexer.generate_index()
    after = _diff_sets()
    return (
        f"Rebuilt index projection at {output}. "
        f"missing_index={len(after['missing_index'])}; extra_index={len(after['extra_index'])}; backup={backup_dir}"
    )


def embedding_backfill_projection(dry_run: bool = True, limit: int | None = None, include_existing: bool = False) -> str:
    """Backfill vectors that are missing, or that were built from input that has since changed.

    Coverage alone does not decide the candidate set any more: a vector can be present and stale,
    so the same input digest the periodic sweep compares is compared here too.  ``--include-existing``
    still means "rebuild everything", which is the force path when the ledger is absent or the
    model changed.
    """
    from vector_lake.embedding_scheduler import (
        embedding_backfill,
        page_bodies_for_keys,
        stale_embedding_keys,
    )

    index_path = get_index_path()
    if not index_path.exists():
        return "index.json not found; run projection-rebuild-index first."
    # The input ledger is a newer table than most databases; converge the schema before reading it
    # rather than reading its absence as "every node is unstamped", which would turn one call into
    # a whole-corpus re-embed.
    init_db()
    index_data = json.loads(index_path.read_text(encoding="utf-8"))
    # index.json no longer carries page bodies, so the embedding corpus is taken from canonical
    # SQLite keyed by page_key -- through the same reader the sweep uses, so the digest written
    # here is the digest the sweep will compare against.
    bodies = page_bodies_for_keys(list((index_data.get("nodes") or {}).keys()))
    stale_inputs = stale_embedding_keys(index_data, bodies)
    result = embedding_backfill(
        index_data,
        dry_run=dry_run,
        limit=limit,
        include_existing=include_existing,
        bodies=bodies,
        extra_keys=stale_inputs,
    )
    before = result.get("coverage_before") or {}
    after = result.get("coverage_after") or before
    lines = [
        "[DRY RUN] Embedding backfill plan" if dry_run else "Embedding backfill complete",
        f"model: {result.get('model')}",
        f"limits: rpm={result.get('rpm')} tpm={result.get('tpm')} utilization={result.get('utilization')}",
        f"effective_limits: rpm={result.get('effective_rpm')} tpm={result.get('effective_tpm')}",
        f"candidates: {result.get('candidates')}",
        f"estimated_requests: {result.get('estimated_requests')}",
        f"estimated_tokens: {result.get('estimated_tokens')}",
        f"max_chars_per_item: {result.get('max_chars_per_item')} max_tokens_per_item: {result.get('max_tokens_per_item')}",
        f"coverage_before: nodes={before.get('nodes')} embedded={before.get('embedded')} missing={before.get('missing')} stale={before.get('stale')}",
        f"stale_inputs: {len(stale_inputs)}",
    ]
    if not dry_run:
        lines.append(f"embedded_this_run: {result.get('embedded')}")
        lines.append(f"failed_batches: {result.get('failed_batches')}")
        lines.append(f"coverage_after: nodes={after.get('nodes')} embedded={after.get('embedded')} missing={after.get('missing')} stale={after.get('stale')}")
    if result.get("skipped"):
        lines.append(f"skipped: {result['skipped']}")
    if result.get("last_error"):
        lines.append(f"last_error: {result['last_error']}")
    return "\n".join(lines)


def _canonical_entity_by_page_key(page_key: str) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT data_json FROM entities WHERE f_page_key = ? LIMIT 1",
        (page_key,),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["data_json"])


def _iso_datetime(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return datetime.now(timezone.utc).isoformat()
    if "T" not in raw and len(raw) == 10:
        return f"{raw}T00:00:00+00:00"
    return raw


def _frontmatter_from_entity(entity: dict) -> dict:
    page_key = str(entity.get("page_key") or entity.get("source_page") or "").replace(".md", "")
    inferred_type = page_key.split("_", 1)[0].lower() if "_" in page_key else "concept"
    entity_type = str(entity.get("type") or entity.get("entity_type") or inferred_type or "concept").lower()
    # An entity row with no category is an unclassified *legacy* node, and the ontology has a
    # marker for exactly that.  This used to write ``[entity_type.capitalize()]`` -- the node
    # type wearing a category's coat -- which put values like ``Source`` and ``Concept`` into
    # pages, none of which are in ``VALID_CATEGORIES``.
    categories = entity.get("categories") or ["Uncategorized"]
    if isinstance(categories, str):
        categories = [categories]
    return {
        "id": entity.get("id") or entity.get("entity_id") or page_key,
        "title": entity.get("title") or entity.get("canonical_name") or page_key,
        "type": entity_type,
        "domain": entity.get("domain") or "General",
        "status": entity.get("status") or "Active",
        "epistemic-status": entity.get("epistemic-status") or "seed",
        "categories": categories,
        "updated": _iso_datetime(entity.get("updated") or entity.get("updated_at")),
        "sources": entity.get("sources") or [],
        "strategic_scope": entity.get("strategic_scope") or "core",
        "evidence_tier": entity.get("evidence_tier") or "primary",
    }


def _body_from_entity(entity: dict, frontmatter: dict) -> str:
    raw_text = str(entity.get("raw_text") or "").strip()
    entity_type = str(frontmatter.get("type") or "concept").lower()
    title = str(frontmatter.get("title") or entity.get("canonical_name") or entity.get("page_key"))
    restored_note = (
        f"{title}：canonical 记录存在，但 Markdown 投影缺失。本页由维护流程从 canonical 元数据恢复，"
        "需要后续补充原始证据与完整编译事实。"
    )
    if entity_type == "source":
        return raw_text or restored_note
    if entity_type == "synthesis":
        return raw_text or (
            "## 核心合成论点 (Core Synthesized Claims)\n\n"
            f"- {restored_note}\n\n"
            "## 支撑拓扑 (Supporting Topology)\n\n"
            "- [mentions:: [[Concept_Agent_Code_Cleanliness]]]\n"
        )
    if raw_text and "## 1. 编译事实" in raw_text and "## 2. 证据时间线" in raw_text:
        return raw_text
    slot = (VALID_H3_SLOTS.get(entity_type) or VALID_H3_SLOTS["concept"])[0]
    restored_at = datetime.now(timezone.utc).date().isoformat()
    return (
        "## 1. 编译事实\n\n"
        f"{slot}\n\n"
        f"{restored_note}\n\n"
        "## 2. 证据时间线\n\n"
        f"- [{restored_at}] [Observation] Markdown projection restored from canonical metadata during maintenance.\n"
    )


def restore_missing_wiki_from_canonical(dry_run: bool = True, limit: int = 10) -> str:
    """Restore Markdown projection files for canonical rows whose Wiki page is missing."""
    diff = _diff_sets()
    page_keys = sorted(diff["extra_canonical"])[: max(1, int(limit))]
    if dry_run:
        return (
            f"[DRY RUN] Would restore {len(page_keys)} of {len(diff['extra_canonical'])} "
            f"canonical-only wiki page(s): {', '.join(page_keys[:10]) if page_keys else '<none>'}"
        )
    if not page_keys:
        return "No canonical-only wiki pages to restore."

    backup_dir = create_maintenance_backup("wiki_restore")
    from vector_lake.mutation_coordinator import execute_mutation_batch

    restored = 0
    skipped: list[str] = []
    failed: list[str] = []
    wiki_dir = get_wiki_dir()
    for page_key in page_keys:
        entity = _canonical_entity_by_page_key(page_key)
        if not entity:
            skipped.append(page_key)
            continue
        path = wiki_dir / f"{page_key}.md"
        if path.exists():
            skipped.append(page_key)
            continue
        frontmatter = _frontmatter_from_entity(entity)
        body = _body_from_entity(entity, frontmatter)
        content = f"---\n{dump_yaml(frontmatter, allow_unicode=True, default_flow_style=False, sort_keys=False)}---\n{body}"
        try:
            # Duplicate-safe: the coordinator writes atomically and registers the
            # projection, so ``missing_index`` drift is repaired in the same step
            # instead of being created by the repair itself.
            execute_mutation_batch(
                [{"filename": f"{page_key}.md", "content": content}],
                validation_mode="schema",
            )
            restored += 1
        except Exception as exc:
            failed.append(f"{page_key} ({type(exc).__name__}: {exc})")
    result = f"Restored {restored} missing wiki page(s) from canonical metadata; backup={backup_dir}"
    if skipped:
        result += f"; skipped={', '.join(skipped[:10])}"
    if failed:
        result += f"; failed={'; '.join(failed[:5])}"
    remaining = _diff_sets()
    if remaining["missing_index"] or remaining["extra_index"]:
        result += (
            "; index projection still drifted "
            f"(missing_index={len(remaining['missing_index'])}, extra_index={len(remaining['extra_index'])}). "
            "Run `projection-rebuild-index --apply` to reconcile it."
        )
    return result
