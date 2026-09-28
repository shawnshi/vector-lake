"""The name-collision ledger: one recorded decision per pair, order-independent and idempotent.

``tool_lint`` reports name *shape*, not duplication, so a pair an operator has read and rejected
came back on every run.  Measured 2026-09-28: 21 pairs reported, 20 distinct entities.  The ledger
holds that decision as a file, so the count moves and a pair can be carved back out.
"""

from vector_lake import name_collision_ledger as ledger


def test_pair_key_is_order_independent_and_strips_the_suffix(isolated_memory):
    assert ledger.pair_key("Concept_A", "Concept_B") == ledger.pair_key("Concept_B.md", "Concept_A.md")
    assert ledger.pair_key("Concept_B", "Concept_A") == "Concept_A::Concept_B"


def test_an_empty_ledger_accepts_nothing(isolated_memory):
    assert ledger.accepted_pair_keys() == set()
    assert ledger.accepted_pair_count() == 0


def test_accepting_a_pair_records_it_once(isolated_memory):
    report = ledger.accept_pairs([("Concept_A", "Concept_B")], reason="distinct entities")

    assert report["added"] == 1
    assert "Concept_A::Concept_B" in ledger.accepted_pair_keys()

    again = ledger.accept_pairs([("Concept_B", "Concept_A")], reason="a second reason cannot win")

    assert again["added"] == 0 and again["skipped"] == 1
    assert ledger.load_ledger()["pairs"]["Concept_A::Concept_B"]["reason"] == "distinct entities"
    assert ledger.accepted_pair_count() == 1


def test_a_broken_ledger_reopens_every_pair(isolated_memory):
    """A corrupt file must fail open: hiding pairs is the one outcome that loses information."""
    ledger.accept_pairs([("Concept_A", "Concept_B")], reason="x")
    ledger.ledger_path().write_text("{not json", encoding="utf-8")

    assert ledger.accepted_pair_keys() == set()


def test_a_dry_run_does_not_write(isolated_memory):
    report = ledger.accept_pairs([("Concept_A", "Concept_B")], reason="x", dry_run=True)

    assert report["added"] == 1
    assert not ledger.ledger_path().exists()
