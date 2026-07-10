import re
import json
from pathlib import Path

class DefenseHookException(Exception):
    pass

VALID_H3_SLOTS = {
    "vendor": ["### 组织架构与商业模式 (Business Model)", "### 核心护城河 (Moat)", "### 市场占位与竞争态势 (Market & Competition)", "### 生态位与战略联盟 (Ecosystem & Alliances)", "### 关键产品线 (Key Products)", "### 核心团队与权力拓扑 (Key Personnel)"],
    "concept": ["### 物理机制 (Mechanism)", "### 适用与失效边界 (Boundaries)", "### 产业落地与代表实例 (Implementations)", "### 演进关联 (Evolution)"],
    "product": ["### 目标客群与应用边界 (Target ICP & Use Cases)", "### 核心价值流 (Value Stream)", "### 部署架构与底层依赖 (Architecture & Dependencies)", "### 商业化与交付模式 (Monetization & Delivery)"],
    "person": ["### 核心权责与控制域 (Mandates & Domain of Control)", "### 关键造物与历史印记 (Key Artifacts & Legacy)", "### 核心主张与商业/技术理念 (Key Stances & Philosophies)", "### 利益纽带与权力拓扑 (Affiliations & Power Topology)"],
    "event": ["### 动因与前置条件 (Catalysts & Preconditions)", "### 核心影响与转折 (Impact)", "### 关键参与方 (Stakeholders)", "### 后续衍生与未决节点 (Fallout & Unresolved Issues)"],
    "policy": ["### 管辖范围与适用对象 (Jurisdiction & Applicability)", "### 核心约束与合规要求 (Compliance Mandates)", "### 奖惩机制与市场影响 (Incentives & Penalties)", "### 演进与废除条件 (Lifecycle)"],
    "standard": ["### 管辖范围与适用对象 (Jurisdiction & Applicability)", "### 核心约束与合规要求 (Compliance Mandates)", "### 奖惩机制与市场影响 (Incentives & Penalties)", "### 演进与废除条件 (Lifecycle)"]
}

def verify_asset(content: str, filename: str, frontmatter: dict, index_path: Path):
    if not filename.endswith(".md"):
        return

    # Skip system files
    if filename in {"index.md", "log.md", "overview.md", "orphan_pages.md", "wiki_link_stats.md", "Synthesis_log.md"}:
        return
        
    # Exclude Source and Synthesis from dual-schema check
    if filename.startswith("Source_") or filename.startswith("Synthesis_"):
        pass # but still check YAML tyranny

    # 1. YAML Tyranny
    for forbidden_key in ["parents", "children", "competes_with"]:
        if forbidden_key in frontmatter:
            raise DefenseHookException(f"SSOT Violation: Topological edge '{forbidden_key}' must not exist in YAML. Use Markdown semantic links instead. Fix the markdown format and try saving again.")
            
    domain = frontmatter.get("domain")
    status = frontmatter.get("status")
    if not domain or not status:
        raise DefenseHookException("Schema Violation: Missing required 'domain' or 'status' in frontmatter. Fix the markdown format and try saving again.")

    tags = frontmatter.get("tags", [])
    if isinstance(tags, list) and len(tags) > 3:
        raise DefenseHookException(f"Taxonomy Violation: Maximum 3 tags allowed, but found {len(tags)}. Compress your understanding. Fix the markdown format and try saving again.")
        
    epistemic_status = frontmatter.get("epistemic-status")
    if epistemic_status and epistemic_status not in ["seed", "sprouting", "evergreen"]:
        raise DefenseHookException(f"Schema Violation: epistemic-status '{epistemic_status}' is invalid. Allowed: seed, sprouting, evergreen. Fix the markdown format and try saving again.")

    # 2. Ontology & Tag Collision
    if tags and index_path.exists():
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                index_data = json.load(f)
            
            entities_in_index = set()
            for node_id, node_data in index_data.get("nodes", {}).items():
                entities_in_index.add(node_data.get("title", "").lower())
                for alias in node_data.get("aliases", []):
                    entities_in_index.add(alias.lower())
                    
            for tag in tags:
                if str(tag).lower() in entities_in_index:
                    raise DefenseHookException(f"Tag Collision: [{tag}] is already an entity and cannot be used as a tag. Use [predicate:: [[Target]]] in the body instead. Fix the markdown format and try saving again.")
        except Exception:
            pass # Index might be missing or corrupted, skip collision check

    # 3. Type-Bound H3 Slots (Only for Dual-Schema entities)
    if not (filename.startswith("Source_") or filename.startswith("Synthesis_")):
        doc_type = frontmatter.get("type", "").lower()
        if doc_type in VALID_H3_SLOTS:
            # Extract section 1
            section_1_match = re.search(r'## 1\. 编译事实.*?(?=## 2\. 证据时间线|---)', content, re.DOTALL)
            if not section_1_match:
                # Tolerate if the file is extremely short or malformed, but ideally we should reject
                # Let's strictly enforce the existence of '## 1. 编译事实'
                raise DefenseHookException("Schema Violation: Missing '## 1. 编译事实' or '## 2. 证据时间线' section divider. Fix the markdown format and try saving again.")
                
            section_1_text = section_1_match.group(0)
            h3_headers = re.findall(r'^###\s+(.*)$', section_1_text, re.MULTILINE)
            
            allowed_slots = VALID_H3_SLOTS[doc_type]
            for header in h3_headers:
                header_cleaned = f"### {header.strip()}"
                if header_cleaned not in allowed_slots:
                    raise DefenseHookException(f"Schema Violation: Invalid H3 header '{header_cleaned}' for type '{doc_type}'. Allowed slots: {allowed_slots}. Fix the markdown format and try saving again.")
