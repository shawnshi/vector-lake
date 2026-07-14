from datetime import datetime, timezone

from filelock import FileLock

from vector_lake.wiki_utils import get_wiki_dir, split_frontmatter
from vector_lake.yaml_utils import dump_yaml


MEMORY_TYPE_MAP = {
    "preference": ("Concept_UserPreferences.md", "User Preferences"),
    "decision": ("Concept_SystemDecisions.md", "System Decisions"),
    "task_state": ("Concept_AgentTaskState.md", "Agent Task State"),
    "fact": ("Concept_OperationalFacts.md", "Operational Facts"),
}


def _new_memory_page(memory_type: str, title: str, now_iso: str) -> str:
    stable_id = f"operational-memory-{memory_type.replace('_', '-')}"
    return f"""---
id: {stable_id}
title: {title}
type: concept
memory_type: {memory_type}
domain: system
status: Active
epistemic-status: seed
categories: [System_Architecture]
updated: {now_iso}
sources: [Operational_Memory]
strategic_scope: core
evidence_tier: derived
topic_cluster: Operational_Memory
---
# {title}

## 1. 编译事实 (Compiled Truth - READ MODEL)
### 物理机制 (Mechanism)
Operational memory entries are compiled into the canonical memory read model.

## 2. 证据时间线 (Evidence Timeline - WRITE MODEL)
"""


def update_operational_memory(memory_type: str, content: str) -> str:
    memory_type = memory_type.lower().strip()
    if memory_type not in MEMORY_TYPE_MAP:
        return f"Error: Invalid memory_type '{memory_type}'. Must be one of: {list(MEMORY_TYPE_MAP.keys())}"
    if not content or not content.strip():
        return "Error: Operational memory content must not be empty."

    filename, title = MEMORY_TYPE_MAP[memory_type]
    target_path = get_wiki_dir() / filename
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    date_text = now.strftime("%Y-%m-%d")

    with FileLock(str(target_path) + ".lock", timeout=10):
        if target_path.exists():
            original = target_path.read_text(encoding="utf-8")
            frontmatter, body = split_frontmatter(original)
            import re
            frontmatter.setdefault("id", f"operational-memory-{memory_type.replace('_', '-')}")
            frontmatter.setdefault("title", title)
            frontmatter["type"] = "concept"
            frontmatter["memory_type"] = memory_type
            frontmatter.setdefault("domain", "system")
            frontmatter["status"] = "Active"
            frontmatter.setdefault("epistemic-status", "seed")
            frontmatter.setdefault("categories", ["System_Architecture"])
            frontmatter["updated"] = now_iso
            frontmatter.setdefault("sources", ["Operational_Memory"])
            frontmatter.setdefault("strategic_scope", "core")
            frontmatter.setdefault("evidence_tier", "derived")
            frontmatter.setdefault("topic_cluster", "Operational_Memory")
            if "### 物理机制 (Mechanism)" not in body:
                body = body.replace(
                    "## 2. 证据时间线",
                    "### 物理机制 (Mechanism)\nOperational memory entries are compiled into the canonical memory read model.\n\n## 2. 证据时间线",
                    1,
                )
            body = re.sub(r"(?m)^- (\[\d{4}-\d{2}-\d{2}\])(?!\s+\[[A-Za-z]+\])", r"- \1 [Observation]", body)
            file_content = f"---\n{dump_yaml(frontmatter, allow_unicode=True, default_flow_style=False, sort_keys=False)}---\n{body.lstrip()}"
        else:
            file_content = _new_memory_page(memory_type, title, now_iso)

        if not file_content.endswith("\n"):
            file_content += "\n"
        clean_content = content.strip().replace("\n", "  \n")
        file_content += f"- [{date_text}] [Observation] {clean_content} (Source: [[Source_Operational-Memory]])\n"

        from vector_lake.mutation_coordinator import execute_mutation_plan
        execute_mutation_plan(filename, content=file_content, is_delete=False)

    return f"Successfully persisted {memory_type} to {filename}; canonical state and outbox intent committed."
