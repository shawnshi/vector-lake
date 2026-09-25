#!/usr/bin/env python3
"""Resolve orphan nodes by establishing semantic topology links in valid H3 slots and acknowledging standalone policies."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from vector_lake.mutation_coordinator import execute_mutation_batch
from vector_lake.wiki_utils import get_meta_dir, get_wiki_dir, read_markdown_file
from vector_lake.yaml_utils import dump_yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("resolve_orphan_nodes")


def build_resolution_plans() -> list[dict]:
    wiki_dir = get_wiki_dir()
    mutations = []

    # 1. Vendor_OpenAI -> [[Concept_OpenAI-Frontier-Safety-Privacy-202608]]
    openai_path = wiki_dir / "Vendor_OpenAI.md"
    if openai_path.exists():
        fm, body, orig = read_markdown_file(openai_path)
        target = "[[Concept_OpenAI-Frontier-Safety-Privacy-202608]]"
        if target not in body:
            line_to_add = "- [[Vendor_OpenAI]] 推进前沿安全与隐私承诺，覆盖网络隔离与思维链监控 [related_to:: [[Concept_OpenAI-Frontier-Safety-Privacy-202608]]] (Source: [[Source_news-2026Q3-intelligence-20260824-briefing-5ca36b43]])"
            slot_marker = "### 核心护城河 (Moat)"
            if slot_marker in body:
                parts = body.split(slot_marker, 1)
                new_body = parts[0] + slot_marker + "\n\n" + line_to_add + parts[1]
                yaml_b = dump_yaml(fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
                mutations.append({
                    "filename": openai_path.name,
                    "content": f"---\n{yaml_b}---\n{new_body.lstrip()}",
                    "old_content": orig,
                })

    # 2. Concept_Agentic-Workflow -> [[Concept_Programmatic-Agent-Skills]], [[Concept_Healthcare-AI-Workflow-Governance-202608]]
    agentic_path = wiki_dir / "Concept_Agentic-Workflow.md"
    if agentic_path.exists():
        fm, body, orig = read_markdown_file(agentic_path)
        t1 = "[[Concept_Programmatic-Agent-Skills]]"
        t2 = "[[Concept_Healthcare-AI-Workflow-Governance-202608]]"
        if t1 not in body or t2 not in body:
            lines = [
                "- [[Concept_Agentic-Workflow]] [related_to:: [[Concept_Programmatic-Agent-Skills]]] (Source: [[Source_youtube-智能体的越狱与收编从语言概率到程序化操作系统的底层跃迁-2026-09-03-fa64b630]])",
                "- [[Concept_Agentic-Workflow]] [related_to:: [[Concept_Healthcare-AI-Workflow-Governance-202608]]] (Source: [[Source_news-2026Q3-intelligence-20260821-briefing-8b4246f2]])"
            ]
            slot_marker = "### 演进关联 (Evolution)"
            if slot_marker in body:
                parts = body.split(slot_marker, 1)
                new_body = parts[0] + slot_marker + "\n\n" + "\n".join(lines) + parts[1]
                yaml_b = dump_yaml(fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
                mutations.append({
                    "filename": agentic_path.name,
                    "content": f"---\n{yaml_b}---\n{new_body.lstrip()}",
                    "old_content": orig,
                })

    # 3. Concept_EHR -> [[Event_VA-EHRM-Federal-EHR-Overview-20260824]]
    ehr_path = wiki_dir / "Concept_EHR.md"
    if ehr_path.exists():
        fm, body, orig = read_markdown_file(ehr_path)
        t_ehr = "[[Event_VA-EHRM-Federal-EHR-Overview-20260824]]"
        if t_ehr not in body:
            line_to_add = "- [[Concept_EHR]] [related_to:: [[Event_VA-EHRM-Federal-EHR-Overview-20260824]]] (Source: [[Source_research-unsupported-43-20260909-va-53b24893]])"
            slot_marker = "### 演进关联 (Evolution)"
            if slot_marker in body:
                parts = body.split(slot_marker, 1)
                new_body = parts[0] + slot_marker + "\n\n" + line_to_add + parts[1]
                yaml_b = dump_yaml(fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
                mutations.append({
                    "filename": ehr_path.name,
                    "content": f"---\n{yaml_b}---\n{new_body.lstrip()}",
                    "old_content": orig,
                })

    # 4. 独立政策与客观独立实体打上 acknowledged-orphan 确权标记
    standalone_nodes = [
        "Concept_US-Rural-HIT-Funding-and-HIPAA-Access-20260827.md",
        "Concept_历史技术债的物理剥离.md",
        "Event_Healthcare-Digital-Briefing-20260824.md",
        "Event_VA-EHRM-Federal-EHR-Overview-20260824.md",
        "Policy_药品全品种全链条信息化追溯要求.md",
        "Policy_长期护理保险支付管理指导意见-2026.md",
        "Vendor_中信医疗.md",
    ]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for name in standalone_nodes:
        p = wiki_dir / name
        if not p.exists():
            continue
        fm, body, orig = read_markdown_file(p)
        if fm.get("topology_status") != "acknowledged-orphan":
            updated_fm = dict(fm)
            updated_fm["topology_status"] = "acknowledged-orphan"
            updated_fm["topology_acknowledged_at"] = today
            updated_fm["topology_review_basis"] = "domain-standalone-policy"
            yaml_b = dump_yaml(updated_fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
            mutations.append({
                "filename": p.name,
                "content": f"---\n{yaml_b}---\n{body.lstrip()}",
                "old_content": orig,
            })

    return mutations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply mutations (default is dry-run)")
    args = parser.parse_args()

    plans = build_resolution_plans()
    print(f"Found {len(plans)} page(s) to update for orphan resolution:")
    for item in plans:
        print(f"  - {item['filename']}")

    if not plans:
        print("No orphan updates required.")
        return 0

    if not args.apply:
        print("\n[DRY RUN] Run with --apply to commit mutations through MutationCoordinator.")
        return 0

    meta_dir = get_meta_dir()
    migrations_dir = meta_dir / "migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")
    rollback_file = migrations_dir / f"resolve-orphans-{today}.rollback.jsonl"

    with open(rollback_file, "w", encoding="utf-8") as handle:
        for item in plans:
            record = {
                "filename": item["filename"],
                "content": item["old_content"],
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"Rollback log written to {rollback_file}")

    mutations = [
        {"filename": item["filename"], "content": item["content"]}
        for item in plans
    ]

    execute_mutation_batch(mutations, validation_mode="schema")
    print(f"Successfully applied {len(plans)} mutations for orphan resolution.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
