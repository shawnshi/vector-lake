import re
import json
from datetime import datetime
from pathlib import Path

class SchemaViolationException(Exception):
    pass

VALID_H3_SLOTS = {
    "vendor": ["### 组织架构与商业模式 (Business Model)", "### 核心护城河 (Moat)", "### 市场占位与竞争态势 (Market & Competition)", "### 生态位与战略联盟 (Ecosystem & Alliances)", "### 关键产品线 (Key Products)", "### 核心团队与权力拓扑 (Key Personnel)"],
    "institution": ["### 机构定位与核心诉求 (Positioning & Needs)", "### 数字化演进路线 (Digital Roadmap)", "### 核心供应商与生态锚定 (Key Suppliers & Lock-ins)", "### 预算流动与财务状况 (Budget & Financial Health)"],
    "concept": ["### 物理机制 (Mechanism)", "### 适用与失效边界 (Boundaries)", "### 产业落地与代表实例 (Implementations)", "### 演进关联 (Evolution)"],
    "product": ["### 目标客群与应用边界 (Target ICP & Use Cases)", "### 临床与管理价值流 (Clinical & Admin Value)", "### 部署架构与底层依赖 (Architecture & Dependencies)", "### 医疗合规与资质壁垒 (Compliance & Certifications)", "### 商业化与交付模式 (Monetization & Delivery)"],
    "person": ["### 核心权责与控制域 (Mandates & Domain of Control)", "### 关键造物与历史印记 (Key Artifacts & Legacy)", "### 核心主张与商业/技术理念 (Key Stances & Philosophies)", "### 利益纽带与权力拓扑 (Affiliations & Power Topology)"],
    "event": ["### 动因与前置条件 (Catalysts & Preconditions)", "### 核心影响与转折 (Impact)", "### 关键参与方 (Stakeholders)", "### 后续衍生与未决节点 (Fallout & Unresolved Issues)"],
    "policy": ["### 管辖范围与适用对象 (Jurisdiction & Applicability)", "### 核心约束与合规要求 (Compliance Mandates)", "### 奖惩机制与市场影响 (Incentives & Penalties)", "### 演进与废除条件 (Lifecycle)"],
    "standard": ["### 管辖范围与适用对象 (Jurisdiction & Applicability)", "### 核心约束与合规要求 (Compliance Mandates)", "### 奖惩机制与市场影响 (Incentives & Penalties)", "### 演进与废除条件 (Lifecycle)"]
}

VALID_TYPES = {"vendor", "institution", "product", "person", "event", "concept", "policy", "standard", "source", "synthesis", "system"}

VALID_CATEGORIES = {
    "Uncategorized",
    "Artificial_Intelligence",
    "Healthcare_IT",
    "Strategy_and_Business",
    "System_Architecture",
    "Philosophy_and_Cognitive",
    "Biomedicine",
    "Policy_and_Governance",
    "Entities_and_Actors"
}

VALID_STATUS = {"Active", "Draft", "Superseded", "Deprecated", "Archived", "Contested"}
VALID_EPISTEMIC_STATUS = {"seed", "sprouting", "evergreen"}

# Metric keys double as a physical unit contract.  Keep legacy keys readable,
# but use the unambiguous keys below for all newly compiled SIR evidence.
CONTROLLED_METRICS = {
    "MedIT_Revenue", "Bids_Won", "Market_Share", "EMR_Level", "CHI_Level", "SLA",
    "Bid_Count", "Bid_Value_CNY", "FHIR_OMOP_Center_Count", "IT_Budget_Change_Pct",
    "Public_Cloud_Deployment_Ratio", "Acceptance_Case_Count", "Engineering_Test_RPS",
    "Engineering_Test_P99_MS", "Engineering_Test_Error_Rate_Pct",
    "GPU_Infrastructure_Cost_CNY", "API_Access_Fee_CNY", "Implementation_Duration_Days",
    "Implementation_Cost_CNY", "Project_Cancellation_Rate_Pct", "SaaS_Value_Share_Pct",
}

INLINE_SOURCE_ANCHOR = re.compile(r"\(Source:\s*\[\[Source_[^\]]+\]\](?:[^)]*)\)")

def validate_schema(frontmatter: dict, body: str, filename: str, index_path: Path = None):
    """
    Validates a Vector Lake Wiki node against the strict constraints of schema.md.
    Raises SchemaViolationException on any failure.
    """
    if not filename.endswith(".md"):
        return

    # Skip system meta files
    if filename in {"index.md", "log.md", "overview.md", "orphan_pages.md", "wiki_link_stats.md", "Synthesis_log.md"}:
        return

    # --- 1. FRONTMATTER VALIDATION ---
    if not isinstance(frontmatter, dict):
        raise SchemaViolationException("Schema Violation: Frontmatter must be a valid YAML object.")

    # 1.1 Required Fields
    # strategic_scope and evidence_tier are highly recommended but we allow legacy files without them
    required_fields = ["id", "title", "type", "domain", "status", "epistemic-status", "categories", "updated", "sources"]
    for field in required_fields:
        if field not in frontmatter:
            # For system files, we can be more lenient
            if filename.startswith("System_") and field in ["domain", "epistemic-status", "sources"]:
                continue
            raise SchemaViolationException(f"Schema Violation: Missing required frontmatter field '{field}'.")
    # 1.2 Type Validation
    doc_type = frontmatter.get("type", "").lower()
    if doc_type not in VALID_TYPES:
        raise SchemaViolationException(f"Schema Violation: Invalid type '{doc_type}'. Must be one of {VALID_TYPES}.")

    # 1.3 Epistemic Status
    epistemic_status = str(frontmatter.get("epistemic-status", "")).lower()
    if epistemic_status and epistemic_status not in VALID_EPISTEMIC_STATUS:
        raise SchemaViolationException(f"Schema Violation: epistemic-status '{epistemic_status}' is invalid. Allowed: {VALID_EPISTEMIC_STATUS}.")

    # 1.4 Status
    status = str(frontmatter.get("status", "")).title()
    if status and status not in VALID_STATUS:
        raise SchemaViolationException(f"Schema Violation: status '{status}' is invalid. Allowed: {VALID_STATUS}.")

    # 1.5 Tags Constraints
    tags = frontmatter.get("tags", [])
    if isinstance(tags, list) and len(tags) > 3:
        raise SchemaViolationException(f"Taxonomy Violation: Maximum 3 tags allowed, but found {len(tags)}.")
        
    # 1.6 Dates
    try:
        if "created" in frontmatter:
            datetime.fromisoformat(str(frontmatter["created"]).replace("Z", "+00:00"))
        datetime.fromisoformat(str(frontmatter["updated"]).replace("Z", "+00:00"))
    except ValueError:
        raise SchemaViolationException("Schema Violation: 'created' and 'updated' must be valid ISO8601 timestamps.")
        
    # 1.7 Tension Edges & STQM
    tension_edges = frontmatter.get("tension_edges", [])
    if tension_edges:
        if not isinstance(tension_edges, list):
            raise SchemaViolationException("Schema Violation: 'tension_edges' must be a list.")
        for te in tension_edges:
            if not isinstance(te, dict) or 'target' not in te or 'polarity' not in te or 'intensity' not in te:
                raise SchemaViolationException("Schema Violation: 'tension_edges' items must contain target, polarity, and intensity.")
            try:
                polarity = float(te['polarity'])
                intensity = float(te['intensity'])
            except ValueError:
                raise SchemaViolationException("Schema Violation: tension_edges polarity and intensity must be floats.")
            if not -1.0 <= polarity <= 1.0:
                raise SchemaViolationException(f"Schema Violation: tension_edges polarity {polarity} out of bounds [-1.0, 1.0].")
            if not 0.0 <= intensity <= 1.0:
                raise SchemaViolationException(f"Schema Violation: tension_edges intensity {intensity} out of bounds [0.0, 1.0].")

    # 1.8 YAML Tyranny
    for forbidden_key in ["parents", "children", "competes_with"]:
        if forbidden_key in frontmatter:
            raise SchemaViolationException(f"SSOT Violation: Topological edge '{forbidden_key}' must not exist in YAML. Use Markdown semantic links instead.")

    # --- 2. FILE NAMING ---
    prefix = filename.split("_")[0]
    if prefix.lower() != doc_type and doc_type != "system":
        raise SchemaViolationException(f"Schema Violation: Filename prefix '{prefix}' does not match frontmatter type '{doc_type}'.")

    # --- 3. BODY SYNTAX VALIDATION ---
    
    # 3.1 Exempted Files (Source, Synthesis, System)
    if prefix in {"Source", "System"}:
        pass
    elif prefix == "Synthesis":
        if "## 核心合成论点 (Core Synthesized Claims)" not in body or "## 支撑拓扑 (Supporting Topology)" not in body:
            raise SchemaViolationException("Schema Violation: Synthesis files must contain '## 核心合成论点 (Core Synthesized Claims)' and '## 支撑拓扑 (Supporting Topology)'.")
    # 3.2 Dual-Schema Entities
    else:
        # Check Dual-Schema Split
        section_1_match = re.search(r'## 1\. 编译事实.*?(?=## 2\. 证据时间线|---|\Z)', body, re.DOTALL)
        if not section_1_match:
            raise SchemaViolationException("Schema Violation: Missing '## 1. 编译事实' section.")
        if "## 2. 证据时间线" not in body:
            raise SchemaViolationException("Schema Violation: Missing '## 2. 证据时间线' section.")

        section_1_text = section_1_match.group(0)
        
        # A simple check could flag standalone pronouns, but structural checks
        # avoid false positives in Chinese.
        # For now, let's just do structural checks to avoid false positives in Chinese.
        
        # H3 Slots
        h3_headers = re.findall(r'^###\s+(.*)$', section_1_text, re.MULTILINE)
        allowed_slots = list(VALID_H3_SLOTS.get(doc_type, []))
        if tension_edges:
            allowed_slots.append("### 认知张力与未决争议 (Controversies & Tensions)")
            
        for header in h3_headers:
            header_cleaned = f"### {header.strip()}"
            if header_cleaned not in allowed_slots:
                raise SchemaViolationException(f"Schema Violation: Invalid H3 header '{header_cleaned}' for type '{doc_type}'. Allowed slots: {allowed_slots}.")
        
        if tension_edges and "### 认知张力与未决争议 (Controversies & Tensions)" not in section_1_text:
            raise SchemaViolationException("Schema Violation: 'tension_edges' defined in YAML but missing '### 认知张力与未决争议 (Controversies & Tensions)' slot.")

        # Metric Constraint
        metric_matches = re.findall(r'\{Metric:\s*([^\}]+)\}', section_1_text)
        for m in metric_matches:
            if m.strip() not in CONTROLLED_METRICS:
                raise SchemaViolationException(f"Schema Violation: Invalid Metric key '{m.strip()}'. Allowed keys: {sorted(CONTROLLED_METRICS)}.")

        # Metrics are hard evidence.  A number without an auditable primary
        # source is not allowed to alter the Compiled Truth read model.
        for line in section_1_text.splitlines():
            if "{Metric:" in line and not INLINE_SOURCE_ANCHOR.search(line):
                raise SchemaViolationException(
                    "Schema Violation: Metric assertions require an inline "
                    "'(Source: [[Source_*]])' anchor."
                )

        # Event Store (Timeline) validation
        section_2_match = re.search(r'## 2\. 证据时间线.*', body, re.DOTALL)
        if section_2_match:
            section_2_text = section_2_match.group(0)
            # Find all bullets
            bullets = re.findall(r'^\s*-\s+(.*)$', section_2_text, re.MULTILINE)
            valid_tags = {"[Release]", "[Pivot]", "[Conflict]", "[Validation]", "[Observation]", "[Decision]", "[Execution]", "[Outcome]"}
            for bullet in bullets:
                # Bypass pure text instructions or quotes
                if bullet.startswith("[YYYY-MM-DD]") or "Event_Tag" in bullet:
                    continue
                # Match strict timeline start
                if not re.match(r'^\[\d{4}-\d{2}-\d{2}\]', bullet):
                    raise SchemaViolationException(f"Schema Violation: Timeline entry '{bullet[:20]}...' must start with [YYYY-MM-DD].")
                
                # Check for tag
                tag_match = re.search(r'^\[\d{4}-\d{2}-\d{2}\]\s+(\[.*?\])', bullet)
                if not tag_match or tag_match.group(1) not in valid_tags:
                    raise SchemaViolationException(f"Schema Violation: Timeline entry must have a valid Event_Tag {valid_tags}. Found in: {bullet[:30]}")

    # --- 4. SEMANTIC LINKS (PREDICATES) ---
    clean_body = re.sub(r'```.*?```', '', body, flags=re.DOTALL)
    clean_body = re.sub(r'`.*?`', '', clean_body)
    
    # Check for invalid predicates in typed links
    # Allowed predicates based on schema.md
    valid_predicates = {
        "is-a", "part-of", "evolved-from", "created", "founded", "authored", "architected",
        "competes-with", "supplies-to", "supplied-by", "blocks", "controls", "manages", "invested-in", "allied-with",
        "integrates-with", "runs-on", "deployed-at", "complies-with", "certified-by",
        "validates", "falsifies", "depends-on", "instantiated-by", "mentions", "related_to", "has_part", "属于", "核心构件", "关联", "提及", "引用", "类似", "parent", "belongs_to", "instance_of", "peer", "see_also", "conflicts-with"
    } # Keeping backward compatible but standardized.
    
    for match in re.finditer(r"\[([^\[\]]+?)::\s*\[\[(.*?)\]\]\]", clean_body):
        predicate = match.group(1).strip()
        if predicate not in valid_predicates:
            # We can be strict and block them
            raise SchemaViolationException(f"Schema Violation: Invalid predicate '{predicate}'.")
            
    # Naked links check: schema says ALL topological relations must use [predicate:: [[Target]]].
    # But RAG sources often use naked links for sources (e.g. (Source: [[Source_X]])).
    # We shouldn't raise exception for (Source: [[Source_X]]).
    
    # Check tag collision if index is passed
    if tags and index_path and index_path.exists():
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
                    raise SchemaViolationException(f"Tag Collision: [{tag}] is already an entity and cannot be used as a tag. Use semantic links instead.")
        except (OSError, json.JSONDecodeError, TypeError, AttributeError):
            pass
