"""Conservative repair tools for explicit Vector Lake governance debt.

The routines in this module never create placeholder knowledge. Broken links are
either redirected to one existing, unambiguous node or converted to plain text
and recorded as acknowledged missing-target debt. Unsupported claims remain
unsupported, but become distinguishable from unreviewed debt after registration.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from vector_lake import db_store, governance_store
from vector_lake.governance_metrics import claim_governance_version, infer_claim_validity
from vector_lake.merge_analysis import normalize_name
from vector_lake.mutation_coordinator import execute_mutation_batch
from vector_lake.tool_lint import _register_link_target, _resolve_link_target
from vector_lake.watchdog_app import process_mutation_outbox_batch
from vector_lake.wiki_utils import (
    WIKI_LINK_PATTERN,
    get_wiki_dir,
    iter_wiki_link_matches,
    markdown_fenced_code_spans,
    read_markdown_file,
)


_LINK = WIKI_LINK_PATTERN
_TEMPORAL_LINK = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TOPOLOGY_STATUS = "acknowledged-orphan"
_MANAGED_STATUSES = {"acknowledged"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_item_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:24]
    return f"gov_{prefix}_{digest}"


def _wiki_files() -> list[Path]:
    return sorted(
        path
        for path in get_wiki_dir().glob("*.md")
        if path.is_file() and path.name not in {"index.md", "log.md", "overview.md"}
    )


def _link_registry() -> tuple[
    dict[str, str],
    dict[str, set[str]],
    dict[str, set[str]],
    dict[str, str],
]:
    exact_map: dict[str, str] = {}
    normalized_map: dict[str, set[str]] = defaultdict(set)
    node_labels: dict[str, set[str]] = defaultdict(set)
    durable_aliases: dict[str, str] = {}
    for path in _wiki_files():
        frontmatter, _, _ = read_markdown_file(path)
        node_key = path.stem
        labels = [node_key, frontmatter.get("title")]
        aliases = frontmatter.get("aliases", [])
        if isinstance(aliases, str):
            aliases = [aliases]
        labels.extend(aliases if isinstance(aliases, list) else [])
        for label in labels:
            if not str(label or "").strip():
                continue
            text = str(label).strip()
            _register_link_target(exact_map, normalized_map, text, node_key)
            normalized = normalize_name(text)
            if normalized:
                node_labels[node_key].add(normalized)

    # The durable alias registry points at entity ids, not page keys. Resolve
    # that join in SQL so the repair does not materialize all entity bodies.
    db_store.init_db()
    for row in db_store.get_connection().execute(
        "SELECT a.key AS alias, json_extract(e.data_json, '$.page_key') AS page_key "
        "FROM alias_registry AS a JOIN entities AS e ON e.entity_id = a.value "
        "WHERE json_extract(e.data_json, '$.page_key') IS NOT NULL"
    ):
        alias = str(row["alias"] or "").strip()
        page_key = str(row["page_key"] or "").strip()
        if not alias or not page_key or page_key not in node_labels:
            continue
        durable_aliases[alias] = page_key
        normalized = normalize_name(alias)
        if normalized:
            node_labels[page_key].add(normalized)
    return exact_map, normalized_map, node_labels, durable_aliases


def _fuzzy_existing_target(
    target: str,
    node_labels: dict[str, set[str]],
    threshold: float,
    margin: float,
) -> tuple[str | None, float, float]:
    target_norm = normalize_name(str(target).strip().strip("[]()（）"))
    if len(target_norm) < 4:
        return None, 0.0, 0.0
    scores: list[tuple[float, str]] = []
    target_numbers = tuple(re.findall(r"\d+", target_norm))
    for node_key, labels in node_labels.items():
        compatible = [
            label
            for label in labels
            if tuple(re.findall(r"\d+", label)) == target_numbers
            and label[:1] == target_norm[:1]
            and (2 * min(len(label), len(target_norm)))
            / (len(label) + len(target_norm))
            >= threshold
        ]
        if not compatible:
            continue
        score = max(SequenceMatcher(None, target_norm, label).ratio() for label in compatible)
        if score >= threshold:
            scores.append((score, node_key))
    scores.sort(key=lambda item: (-item[0], item[1]))
    if not scores:
        return None, 0.0, 0.0
    best_score, best_node = scores[0]
    second_score = scores[1][0] if len(scores) > 1 else 0.0
    if best_score < threshold or best_score - second_score < margin:
        return None, best_score, second_score
    return best_node, best_score, second_score


def analyze_broken_link_governance(
    fuzzy_threshold: float = 0.93,
    fuzzy_margin: float = 0.05,
) -> dict:
    """Classify every currently unresolved Wiki link without changing state."""
    exact_map, normalized_map, node_labels, durable_aliases = _link_registry()
    occurrences: list[dict] = []
    mappings: dict[str, dict] = {}
    missing: dict[str, dict] = {}
    for path in _wiki_files():
        _, _, content = read_markdown_file(path)
        for match in iter_wiki_link_matches(content):
            target = match.group(1).strip().replace(".md", "")
            if not target or _TEMPORAL_LINK.fullmatch(target):
                continue
            if _resolve_link_target(target, exact_map, normalized_map):
                continue
            mapped = durable_aliases.get(target)
            score = 1.0 if mapped else 0.0
            second_score = 0.0
            if not mapped:
                mapped, score, second_score = _fuzzy_existing_target(
                    target,
                    node_labels,
                    float(fuzzy_threshold),
                    float(fuzzy_margin),
                )
            record = {
                "filename": path.name,
                "target": target,
                "label": match.group(2),
            }
            occurrences.append(record)
            if mapped:
                entry = mappings.setdefault(
                    target,
                    {
                        "target": target,
                        "mapped_to": mapped,
                        "score": round(score, 6),
                        "second_score": round(second_score, 6),
                        "files": set(),
                        "occurrences": 0,
                    },
                )
            else:
                entry = missing.setdefault(
                    target,
                    {"target": target, "files": set(), "occurrences": 0},
                )
            entry["files"].add(path.name)
            entry["occurrences"] += 1
    for group in (mappings, missing):
        for entry in group.values():
            entry["files"] = sorted(entry["files"])
    return {
        "occurrences": len(occurrences),
        "unique_targets": len(mappings) + len(missing),
        "mapped_occurrences": sum(item["occurrences"] for item in mappings.values()),
        "mapped_targets": sorted(mappings.values(), key=lambda item: item["target"]),
        "missing_occurrences": sum(item["occurrences"] for item in missing.values()),
        "missing_targets": sorted(missing.values(), key=lambda item: item["target"]),
    }


def _backup_pages(paths: list[Path], backup_dir: str | Path) -> Path:
    root = Path(backup_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    for path in paths:
        shutil.copy2(path, root / path.name)
    manifest = {
        "created_at": _utc_now(),
        "source": str(get_wiki_dir()),
        "files": [path.name for path in paths],
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return root


def _commit_page_mutations(
    mutations: list[dict],
    origin: str,
    batch_size: int = 200,
) -> dict:
    committed = 0
    completed = 0
    failed = 0
    size = max(1, min(250, int(batch_size)))
    for offset in range(0, len(mutations), size):
        batch = mutations[offset : offset + size]
        execute_mutation_batch(batch, validation_mode="schema", origin=origin)
        committed += len(batch)
        outbox = process_mutation_outbox_batch(limit=max(1, len(batch)))
        completed += int(outbox.get("completed", 0))
        failed += int(outbox.get("failed", 0))
    return {"committed": committed, "outbox_completed": completed, "outbox_failed": failed}


def repair_broken_link_governance(
    dry_run: bool = True,
    backup_dir: str = "",
    fuzzy_threshold: float = 0.93,
    fuzzy_margin: float = 0.05,
) -> dict:
    """Repair unresolved links without creating empty knowledge nodes."""
    plan = analyze_broken_link_governance(fuzzy_threshold, fuzzy_margin)
    if dry_run:
        return {"dry_run": True, **plan}
    if not backup_dir:
        raise ValueError("Live link governance requires an explicit backup_dir.")

    mapping = {item["target"]: item["mapped_to"] for item in plan["mapped_targets"]}
    missing = {item["target"]: item for item in plan["missing_targets"]}
    mutations: list[dict] = []
    changed_paths: list[Path] = []

    def replace(match: re.Match) -> str:
        target = match.group(1).strip().replace(".md", "")
        label = match.group(2)
        if target in mapping:
            mapped = mapping[target]
            return f"[[{mapped}|{label}]]" if label else f"[[{mapped}]]"
        if target in missing:
            return str(label or target).strip().strip("[]")
        return match.group(0)

    for path in _wiki_files():
        _, _, content = read_markdown_file(path)
        pieces = []
        cursor = 0
        for match in iter_wiki_link_matches(content):
            pieces.append(content[cursor:match.start()])
            pieces.append(replace(match))
            cursor = match.end()
        pieces.append(content[cursor:])
        updated = "".join(pieces)
        if updated == content:
            continue
        mutations.append({"filename": path.name, "content": updated})
        changed_paths.append(path)

    backup_root = _backup_pages(changed_paths, backup_dir)
    result = _commit_page_mutations(mutations, "broken-link-governance") if mutations else {
        "committed": 0,
        "outbox_completed": 0,
        "outbox_failed": 0,
    }
    created = 0
    now = _utc_now()
    due_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    for target, item in missing.items():
        created += int(
            governance_store.upsert_governance_item(
                {
                    "item_id": _stable_item_id("missing_link", target.casefold()),
                    "type": "missing-link-target",
                    "status": "acknowledged",
                    "title": f"Missing link target: {target}",
                    "target_label": target,
                    "affected_pages": item["files"],
                    "occurrences": item["occurrences"],
                    "resolution": "converted-to-text",
                    "source": "broken-link-governance",
                    "search_queries": [target],
                    "acknowledged_at": now,
                    "owner": "vector-lake-governance",
                    "due_at": due_at,
                },
                insert_only=True,
            )
        )
    return {
        "dry_run": False,
        **plan,
        **result,
        "changed_pages": len(changed_paths),
        "governance_items_created": created,
        "backup": str(backup_root),
    }


def register_missing_link_debt(dry_run: bool = True) -> dict:
    """Bind acknowledged missing-target debt to a current owner and review date."""
    db_store.init_db()
    rows = db_store.get_connection().execute(
        "SELECT data_json FROM governance_queue "
        "WHERE json_extract(data_json, '$.type') = 'missing-link-target' "
        "AND json_extract(data_json, '$.status') = 'acknowledged'"
    ).fetchall()
    now_dt = datetime.now(timezone.utc)
    pending = []
    for row in rows:
        item = json.loads(row["data_json"])
        try:
            due_at = datetime.fromisoformat(str(item.get("due_at") or "").replace("Z", "+00:00"))
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=timezone.utc)
        except ValueError:
            due_at = None
        if not (
            str(item.get("owner") or "").strip()
            and due_at is not None
            and due_at >= now_dt
        ):
            pending.append(item)
    if dry_run:
        return {
            "dry_run": True,
            "missing_link_targets": len(rows),
            "already_managed": len(rows) - len(pending),
            "to_register": len(pending),
        }

    due_at = (now_dt + timedelta(days=30)).isoformat()
    updated = 0
    for item in pending:
        item["owner"] = "vector-lake-governance"
        item["due_at"] = due_at
        item.setdefault("acknowledged_at", now_dt.isoformat())
        updated += int(governance_store.upsert_governance_item(item, insert_only=False))
    return {
        "dry_run": False,
        "missing_link_targets": len(rows),
        "already_managed": len(rows) - len(pending),
        "registered": updated,
        "due_at": due_at,
    }


def _unreviewed_orphans() -> list[Path]:
    exact_map, normalized_map, _, _ = _link_registry()
    inbound = Counter()
    parsed: dict[Path, dict] = {}
    for path in _wiki_files():
        frontmatter, _, content = read_markdown_file(path)
        parsed[path] = frontmatter
        for match in iter_wiki_link_matches(content):
            target = match.group(1).strip().replace(".md", "")
            resolved = _resolve_link_target(target, exact_map, normalized_map)
            if resolved:
                inbound[resolved] += 1
    return [
        path
        for path, frontmatter in parsed.items()
        if not path.name.startswith("Source_")
        and inbound[path.stem] == 0
        and str(frontmatter.get("topology_status", "")).strip().lower() != _TOPOLOGY_STATUS
    ]


def review_orphan_entry_points(
    dry_run: bool = True,
    backup_dir: str = "",
) -> dict:
    """Acknowledge topology debt without claiming that every orphan is intentional."""
    paths = _unreviewed_orphans()
    if dry_run:
        return {"dry_run": True, "orphan_pages": len(paths), "sample": [p.name for p in paths[:20]]}
    if not backup_dir:
        raise ValueError("Live orphan review requires an explicit backup_dir.")
    backup_root = _backup_pages(paths, backup_dir)
    acknowledged_at = date.today().isoformat()
    due_at = (date.today() + timedelta(days=30)).isoformat()
    mutations = []
    for path in paths:
        content = path.read_text(encoding="utf-8")
        closing = re.search(r"\r?\n---(?:\r?\n|$)", content[4:])
        if not content.startswith("---") or closing is None:
            raise ValueError(f"Cannot annotate orphan without valid frontmatter: {path.name}")
        header_end = 4 + closing.start()
        header = content[:header_end]
        suffix = content[header_end:].lstrip("\r\n")
        header = re.sub(
            r"(?m)^topology_(?:status|reviewed_at|acknowledged_at|review_basis|review_owner|review_due):[^\r\n]*(?:\r?\n|$)",
            "",
            header,
        )
        addition = (
            f"topology_status: {_TOPOLOGY_STATUS}\n"
            f"topology_acknowledged_at: '{acknowledged_at}'\n"
            "topology_review_basis: no-resolvable-inbound-links\n"
            "topology_review_owner: vector-lake-governance\n"
            f"topology_review_due: '{due_at}'\n"
        )
        updated = header.rstrip("\r\n") + "\n" + addition + suffix
        mutations.append({"filename": path.name, "content": updated})
    result = _commit_page_mutations(mutations, "orphan-entry-point-review") if mutations else {
        "committed": 0,
        "outbox_completed": 0,
        "outbox_failed": 0,
    }
    return {
        "dry_run": False,
        "acknowledged_pages": len(paths),
        **result,
        "backup": str(backup_root),
    }


def register_unsupported_claim_debt(dry_run: bool = True) -> dict:
    """Register unsupported canonical claims as acknowledged evidence debt."""
    db_store.init_db()
    rows = []
    for row in db_store.get_connection().execute("SELECT data_json FROM claims"):
        claim = json.loads(row["data_json"])
        if infer_claim_validity(claim).get("validity_state") == "unsupported":
            rows.append(claim)
    existing = {
        str(item.get("claim_id") or ""): item
        for row in db_store.get_connection().execute(
            "SELECT data_json FROM governance_queue "
            "WHERE json_extract(data_json, '$.type') = 'evidence-gap' "
            "AND json_extract(data_json, '$.status') = 'acknowledged'"
        )
        for item in [json.loads(row["data_json"])]
        if item.get("claim_id")
    }
    pending = []
    now_dt = datetime.now(timezone.utc)
    for claim in rows:
        item = existing.get(str(claim.get("claim_id") or "")) or {}
        try:
            due_at = datetime.fromisoformat(str(item.get("due_at") or "").replace("Z", "+00:00"))
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=timezone.utc)
        except ValueError:
            due_at = None
        if not (
            str(item.get("owner") or "").strip()
            and due_at is not None
            and due_at >= now_dt
            and str(item.get("claim_version") or "") == claim_governance_version(claim)
        ):
            pending.append(claim)
    if dry_run:
        return {
            "dry_run": True,
            "unsupported_claims": len(rows),
            "already_managed": len(rows) - len(pending),
            "to_register": len(pending),
        }
    created = 0
    now = _utc_now()
    due_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    for claim in pending:
        claim_id = str(claim.get("claim_id"))
        locator = claim.get("locator") if isinstance(claim.get("locator"), dict) else {}
        page_key = str(locator.get("page_key") or claim.get("source_page") or "")
        created += int(
            governance_store.upsert_governance_item(
                {
                    "item_id": _stable_item_id("evidence_gap", claim_id),
                    "type": "evidence-gap",
                    "status": "acknowledged",
                    "title": f"Evidence gap for {claim_id}",
                    "claim_id": claim_id,
                    "claim_version": claim_governance_version(claim),
                    "claim_text": str(claim.get("claim_text") or "")[:500],
                    "affected_pages": [page_key] if page_key else [],
                    "reason": "missing_evidence_and_source",
                    "resolution": "research-required",
                    "owner": "vector-lake-governance",
                    "due_at": due_at,
                    "source": "unsupported-claim-governance",
                    "search_queries": [str(claim.get("claim_text") or "")[:200]],
                    "acknowledged_at": now,
                },
                insert_only=False,
            )
        )
    return {
        "dry_run": False,
        "unsupported_claims": len(rows),
        "already_managed": len(rows) - len(pending),
        "registered": created,
    }


def restore_fenced_code_from_backup(
    source_backup_dir: str,
    repair_report_path: str,
    dry_run: bool = True,
    backup_dir: str = "",
) -> dict:
    """Undo only fenced-block changes exactly explained by one repair report."""
    source_root = Path(source_backup_dir).expanduser().resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"Source backup directory does not exist: {source_root}")
    report_path = Path(repair_report_path).expanduser().resolve()
    if not report_path.is_file():
        raise FileNotFoundError(f"Repair report does not exist: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    mapping = {
        str(item["target"]): str(item["mapped_to"])
        for item in report.get("mapped_targets", [])
    }
    missing = {str(item["target"]) for item in report.get("missing_targets", [])}

    def old_repair(match: re.Match) -> str:
        target = match.group(1).strip().replace(".md", "")
        label = match.group(2)
        if target in mapping:
            return f"[[{mapping[target]}|{label}]]" if label else f"[[{mapping[target]}]]"
        if target in missing:
            return str(label or target).strip().strip("[]")
        return match.group(0)

    mutations = []
    changed_paths = []
    restored_blocks = 0
    for original_path in sorted(source_root.glob("*.md")):
        live_path = get_wiki_dir() / original_path.name
        if not live_path.exists():
            continue
        original = original_path.read_text(encoding="utf-8")
        live = live_path.read_text(encoding="utf-8")
        original_spans = markdown_fenced_code_spans(original)
        live_spans = markdown_fenced_code_spans(live)
        if len(original_spans) != len(live_spans):
            raise RuntimeError(
                f"Fenced-code structure changed for {original_path.name}: "
                f"backup={len(original_spans)} live={len(live_spans)}"
            )
        replacements = []
        for original_span, live_span in zip(original_spans, live_spans):
            original_block = original[original_span[0] : original_span[1]]
            live_block = live[live_span[0] : live_span[1]]
            if original_block != live_block:
                expected_repaired = _LINK.sub(old_repair, original_block)
                if live_block != expected_repaired:
                    raise RuntimeError(
                        f"Refusing to overwrite a fenced block with unexplained edits: "
                        f"{original_path.name}"
                    )
                replacements.append((live_span, original_block))
        if not replacements:
            continue
        updated = live
        for (start, end), original_block in reversed(replacements):
            updated = updated[:start] + original_block + updated[end:]
        mutations.append({"filename": live_path.name, "content": updated})
        changed_paths.append(live_path)
        restored_blocks += len(replacements)
    if dry_run:
        return {
            "dry_run": True,
            "changed_pages": len(changed_paths),
            "restored_blocks": restored_blocks,
        }
    if not backup_dir:
        raise ValueError("Live fenced-code restoration requires an explicit backup_dir.")
    backup_root = _backup_pages(changed_paths, backup_dir)
    result = _commit_page_mutations(mutations, "fenced-code-provenance-restore") if mutations else {
        "committed": 0,
        "outbox_completed": 0,
        "outbox_failed": 0,
    }
    return {
        "dry_run": False,
        "changed_pages": len(changed_paths),
        "restored_blocks": restored_blocks,
        **result,
        "backup": str(backup_root),
    }


def reconcile_missing_link_items_from_backup(
    source_backup_dir: str,
    dry_run: bool = True,
) -> dict:
    """Remove code-only missing-link debt and retain original body-link counts."""
    source_root = Path(source_backup_dir).expanduser().resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"Source backup directory does not exist: {source_root}")
    db_store.init_db()
    rows = db_store.get_connection().execute(
        "SELECT item_id, data_json FROM governance_queue "
        "WHERE json_extract(data_json, '$.type') = 'missing-link-target' "
        "AND json_extract(data_json, '$.source') = 'broken-link-governance'"
    ).fetchall()
    items = [(str(row["item_id"]), json.loads(row["data_json"])) for row in rows]
    target_items = {str(item.get("target_label") or ""): (item_id, item) for item_id, item in items}
    occurrences: dict[str, dict] = defaultdict(lambda: {"count": 0, "files": set()})
    for path in sorted(source_root.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        for match in iter_wiki_link_matches(content):
            target = match.group(1).strip().replace(".md", "")
            if target not in target_items:
                continue
            occurrences[target]["count"] += 1
            occurrences[target]["files"].add(path.name)
    remove_ids = [item_id for target, (item_id, _) in target_items.items() if target not in occurrences]
    update_rows = {
        target: {
            "occurrences": data["count"],
            "affected_pages": sorted(data["files"]),
        }
        for target, data in occurrences.items()
    }
    if dry_run:
        return {
            "dry_run": True,
            "existing_items": len(items),
            "retained_body_targets": len(update_rows),
            "remove_code_only_targets": len(remove_ids),
        }
    for target, updates in update_rows.items():
        governance_store.update_governance_item(target_items[target][0], updates)
    if remove_ids:
        placeholders = ",".join("?" for _ in remove_ids)
        with db_store.transaction():
            db_store.get_connection().execute(
                f"DELETE FROM governance_queue WHERE item_id IN ({placeholders})",
                tuple(remove_ids),
            )
    return {
        "dry_run": False,
        "existing_items": len(items),
        "retained_body_targets": len(update_rows),
        "removed_code_only_targets": len(remove_ids),
    }
