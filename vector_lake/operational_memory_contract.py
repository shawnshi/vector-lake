"""Pure classification contract for governed operational memory."""

MEMORY_TYPE_MAP = {
    "preference": ("Concept_UserPreferences.md", "User Preferences"),
    "decision": ("Concept_SystemDecisions.md", "System Decisions"),
    "task_state": ("Concept_AgentTaskState.md", "Agent Task State"),
    "fact": ("Concept_OperationalFacts.md", "Operational Facts"),
}

OPERATIONAL_SOURCE_MARKERS = {"operational_memory", "source_operational-memory"}


def normalized_memory_type(value) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def is_operational_source_marker(value) -> bool:
    if not isinstance(value, str):
        return False
    return value.strip().lower().removesuffix(".md") in OPERATIONAL_SOURCE_MARKERS


def is_governed_operational_claim(claim: dict, memory_type: str | None = None) -> bool:
    """Return whether a claim satisfies the legacy-compatible governed contract.

    This is classification provenance, not authentication: the fixed page, matching
    explicit kind, and an extractor-observed source marker must all agree.
    """
    explicit = normalized_memory_type(claim.get("memory_type"))
    candidate = normalized_memory_type(memory_type) or explicit
    if not candidate or explicit != candidate or candidate not in MEMORY_TYPE_MAP:
        return False
    expected_page = MEMORY_TYPE_MAP[candidate][0]
    if claim.get("source_page") != expected_page:
        return False
    if "operational_memory_provenance" in claim:
        return claim.get("operational_memory_provenance") is True
    raw_markers = claim.get("inline_sources") or claim.get("source_refs") or []
    if isinstance(raw_markers, str):
        raw_markers = [raw_markers]
    return any(is_operational_source_marker(value) for value in raw_markers)


def effective_routing_type(memory: dict, claim: dict | None = None) -> str:
    stored = normalized_memory_type(memory.get("memory_type"))
    if stored == "fact":
        return "fact"
    if not isinstance(claim, dict):
        return "fact"
    if str(claim.get("status") or "Active").casefold() != "active":
        return "fact"
    if str(claim.get("validity_state") or "").casefold() in {
        "archived", "superseded", "expired", "conflicted", "retracted", "deleted"
    }:
        return "fact"
    if (
        not memory.get("source_claim_id")
        or claim.get("claim_id") != memory.get("source_claim_id")
        or claim.get("claim_text") != memory.get("text")
        or claim.get("source_page") != memory.get("source_page")
    ):
        return "fact"
    return stored if is_governed_operational_claim(claim, stored) else "fact"
