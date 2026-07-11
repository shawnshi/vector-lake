import json

from vector_lake.purpose_contract import (
    PurposeContractError,
    load_purpose_contract,
    review_sir_lifecycle,
)


def review_strategic_purpose(as_of: str = "") -> str:
    """Return due SIR review proposals without changing the Wiki or governance queue."""
    try:
        contract = load_purpose_contract()
        proposals = review_sir_lifecycle(as_of or None, contract)
    except (PurposeContractError, ValueError) as exc:
        return f"Strategic purpose contract is invalid: {exc}"
    return json.dumps({
        "purpose_version": contract["purpose_version"],
        "as_of": as_of or "today",
        "proposals": proposals,
    }, ensure_ascii=False, indent=2)
