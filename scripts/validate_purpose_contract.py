"""Deterministic validation gate for the Vector Lake strategic-purpose contract."""

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vector_lake.purpose_contract import (  # noqa: E402 - import after repo bootstrap
    PurposeContractError,
    build_synthesis_proposals,
    load_purpose_contract,
    render_strategy_directive,
    review_sir_lifecycle,
    validate_ingest_payload,
)
from vector_lake.schema_validator import (  # noqa: E402 - import after repo bootstrap
    SchemaViolationException,
    validate_schema,
)


def main() -> int:
    from vector_lake.runtime_paths import bootstrap_runtime_paths

    bootstrap_runtime_paths(caller="Purpose contract validator")
    contract = load_purpose_contract()
    assert contract["purpose_version"] == "12.1"
    assert "SIR-1" in render_strategy_directive(contract)

    content = """---
id: strategic_probe
title: Strategic Probe
type: institution
domain: Medical_IT
status: Active
epistemic-status: seed
categories: [Healthcare_IT]
updated: '2026-07-11T00:00:00'
sources: [Source_Strategic-Probe]
strategic_scope: core
evidence_tier: engineering-performance
---
## 1. 编译事实 (Compiled Truth)
### 机构定位与核心诉求 (Positioning & Needs)
- {Metric: Engineering_Test_RPS} [[Institution_Strategic-Probe]] 5000 (Source: [[Source_Strategic-Probe]])
## 2. 证据时间线 (Timeline - EVENT STORE)
- [2026-07-11] [Observation] Probe entry (Source: [[Source_Strategic-Probe]])
"""
    frontmatter = {
        "id": "strategic_probe", "title": "Strategic Probe", "type": "institution", "domain": "Medical_IT",
        "status": "Active", "epistemic-status": "seed", "categories": ["Healthcare_IT"],
        "updated": "2026-07-11T00:00:00", "sources": ["Source_Strategic-Probe"],
    }
    validate_schema(frontmatter, content.split("---\n", 2)[2], "Institution_Strategic-Probe.md")
    validate_ingest_payload([{"filename": "Institution_Strategic-Probe.md", "content": content}], contract)

    try:
        validate_schema(frontmatter, content.replace(" (Source: [[Source_Strategic-Probe]])", "").split("---\n", 2)[2], "Institution_Strategic-Probe.md")
    except SchemaViolationException:
        pass
    else:
        raise AssertionError("Metric without an inline Source_* anchor was accepted.")

    try:
        validate_ingest_payload([{"filename": "Institution_Strategic-Probe.md", "content": content.replace("strategic_scope: core\n", "")}], contract)
    except PurposeContractError:
        pass
    else:
        raise AssertionError("Ingest node without strategic_scope was accepted.")

    proposals = build_synthesis_proposals([
        {"filename": "Concept_A.md", "sources": ["Source_A"], "tension_edges": [{"target": "Concept_Cloud", "intensity": 0.80}]},
        {"filename": "Concept_B.md", "sources": ["Source_B"], "tension_edges": [{"target": "Concept_Cloud", "intensity": 0.90}]},
    ], contract)
    assert len(proposals) == 1 and proposals[0]["type"] == "Synthesis-Proposal"
    active_sirs = [
        sir for sir in contract["sir_registry"]
        if str(sir["status"]).lower() == "active"
    ]
    if active_sirs:
        latest_review = max(
            date.fromisoformat(str(sir["review_after"])) for sir in active_sirs
        )
        review_probe = (latest_review + timedelta(days=1)).isoformat()
        assert len(review_sir_lifecycle(review_probe, contract)) == len(active_sirs)
    else:
        assert review_sir_lifecycle(contract=contract) == []
    print("purpose-contract validation: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
