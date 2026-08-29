import os
import random
import sqlite3
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import pytest

from vector_lake import db_store, governance_store, wiki_utils
from vector_lake.tool_search import build_memory_packet, search_vector_lake


def _mixed_memory_results():
    return [
        {
            "memory_id": "fact-result",
            "memory_key": "fact_result",
            "memory_type": "fact",
            "validity_state": "active",
            "retrieval_score": 1.0,
            "text": "Fact-only result.",
            "source_claim_id": "claim-fact",
        },
        {
            "memory_id": "preference-result",
            "memory_key": "preference_result",
            "memory_type": "preference",
            "validity_state": "active",
            "retrieval_score": 0.9,
            "text": "Preference must not leak into fact mode.",
            "source_claim_id": "claim-preference",
        },
    ]


def test_fact_mode_is_fact_only_and_claim_is_an_explicit_alias(monkeypatch):
    observed_memory_types = []

    def fake_search(*_args, memory_types=None, **_kwargs):
        observed_memory_types.append(memory_types)
        return _mixed_memory_results()

    monkeypatch.setattr(
        governance_store,
        "search_operational_memory",
        fake_search,
    )

    fact_result = search_vector_lake("query", mode="fact")
    claim_result = search_vector_lake("query", mode="claim")

    assert observed_memory_types == [["fact"], ["fact"]]
    assert "Fact-only result" in fact_result
    assert "Preference must not leak" not in fact_result
    assert "deprecat" not in fact_result.casefold()
    assert "DEPRECATION / ACTUAL SEMANTICS" in claim_result
    assert "operational-memory facts" in claim_result
    assert "not canonical Claim records" in claim_result
    assert "Preference must not leak" not in claim_result


def test_claim_alias_xml_is_well_formed_and_machine_readable(monkeypatch):
    monkeypatch.setattr(
        governance_store,
        "search_operational_memory",
        lambda *_args, **_kwargs: _mixed_memory_results(),
    )

    root = ET.fromstring(
        search_vector_lake("query", mode="claim", as_xml=True)
    )

    assert root.tag == "VectorLakeSearchResponse"
    assert root.find("SemanticReadinessEnvelope") is not None
    compatibility = root.find("SearchCompatibility")
    assert compatibility is not None
    assert compatibility.attrib == {
        "RequestedMode": "claim",
        "EffectiveMode": "fact",
        "Deprecated": "true",
        "ActualSemantics": "operational_memory_fact_not_canonical_claim",
    }
    assert "not canonical Claim records" in compatibility.findtext("Warning", "")
    memory_items = compatibility.findall("./MemoryResults/Memory_Item")
    assert len(memory_items) == 1
    assert memory_items[0].attrib["Type"] == "fact"


def _reference_memory_relevance(memory, terms):
    haystacks = {
        "key": str(memory.get("memory_key", "")).lower(),
        "text": str(memory.get("text", "")).lower(),
        "page": str(memory.get("source_page", "")).lower(),
        "type": str(memory.get("memory_type", "")).lower(),
    }
    score = 0.0
    for term in terms:
        if term in haystacks["key"]:
            score += 4.0
        if term in haystacks["text"]:
            score += 3.0
        if term in haystacks["page"]:
            score += 1.0
        if term in haystacks["type"]:
            score += 1.0
    return score


class TestOperationalMemoryExactMatcher(unittest.TestCase):
    def test_overlapping_and_multilingual_terms_match_reference(self):
        terms = ["a", "aa", "aaa", "甲", "甲乙", "乙", "🙂", "ß"]
        memory = {
            "memory_key": "aaaa-甲乙",
            "text": "AA甲乙🙂",
            "source_page": "Concept_ß甲",
            "memory_type": "Fact",
        }
        matcher = governance_store._ExactTermMatcher(terms)

        self.assertEqual(
            governance_store._memory_relevance(memory, terms, matcher=matcher),
            _reference_memory_relevance(memory, terms),
        )

    def test_regex_scan_preserves_noncontained_overlaps(self):
        terms = ["aba", "abab", "bab", "甲乙丙", "乙丙丁", "丙丁戊", "🙂ab"]
        memory = {
            "memory_key": "ababa",
            "text": "甲乙丙丁戊🙂ab",
            "source_page": "Concept_abab",
            "memory_type": "fact",
        }
        matcher = governance_store._ExactTermMatcher(terms)

        self.assertIsNone(matcher._direct_masks)
        self.assertEqual(
            governance_store._memory_relevance(memory, terms, matcher=matcher),
            _reference_memory_relevance(memory, terms),
        )

    def test_randomized_exact_matcher_preserves_substring_scoring(self):
        rng = random.Random(20260727)
        alphabet = "abcxyz甲乙丙丁戊己庚辛🙂ß"
        fields = ("memory_key", "text", "source_page", "memory_type")

        for case_index in range(300):
            memory = {
                field: "".join(
                    rng.choice(alphabet) for _ in range(rng.randint(0, 96))
                )
                for field in fields
            }
            corpus = "".join(str(memory[field]).lower() for field in fields)
            terms = []
            for _ in range(rng.randint(13, 64)):
                if len(corpus) >= 3 and rng.random() < 0.75:
                    start = rng.randrange(len(corpus) - 2)
                    width = rng.randint(3, min(8, len(corpus) - start))
                    terms.append(corpus[start : start + width])
                else:
                    terms.append(
                        "".join(
                            rng.choice(alphabet)
                            for _ in range(rng.randint(1, 8))
                        ).lower()
                    )
            matcher = governance_store._ExactTermMatcher(terms)

            with self.subTest(case=case_index):
                self.assertEqual(
                    governance_store._memory_relevance(
                        memory,
                        terms,
                        matcher=matcher,
                    ),
                    _reference_memory_relevance(memory, terms),
                )


    def test_query_limits_fail_before_matcher_construction(self):
        with mock.patch.object(
            governance_store,
            "_query_terms",
            side_effect=AssertionError("oversized query should fail first"),
        ):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                governance_store._bounded_memory_query_terms(
                    "x" * (governance_store._MEMORY_QUERY_CHAR_LIMIT + 1)
                )

        with self.assertRaisesRegex(ValueError, "term longer"):
            governance_store._bounded_memory_query_terms(
                "x" * (governance_store._MEMORY_QUERY_TERM_CHAR_LIMIT + 1)
            )

    def test_matcher_pattern_budget_falls_back_without_changing_terms(self):
        terms = [
            f"term{index:03d}-" + ("x" * 80)
            for index in range(100)
        ]

        self.assertGreater(
            sum(map(len, terms)),
            governance_store._MEMORY_MATCHER_PATTERN_CHAR_LIMIT,
        )
        self.assertIsNone(governance_store._memory_term_matcher(terms))


class TestOperationalMemory(unittest.TestCase):
    def setUp(self):
        self.previous_memory_dir = os.environ.get("VECTOR_LAKE_MEMORY_DIR")
        self._private_temp_root = None
        if self.previous_memory_dir:
            self.test_root = Path(self.previous_memory_dir)
        else:
            self._private_temp_root = tempfile.TemporaryDirectory(
                prefix="vector-lake-operational-memory-"
            )
            self.test_root = Path(self._private_temp_root.name)
            os.environ["VECTOR_LAKE_MEMORY_DIR"] = str(self.test_root)
        self.test_root.mkdir(parents=True, exist_ok=True)
        self.meta_dir = self.test_root / "wiki" / ".meta"
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        db_store.close_all_connections()
        wiki_utils._META_DIR_CACHE = self.meta_dir
        db_store.init_db()

    def tearDown(self):
        db_store.close_all_connections()
        if self.previous_memory_dir is None:
            os.environ.pop("VECTOR_LAKE_MEMORY_DIR", None)
        else:
            os.environ["VECTOR_LAKE_MEMORY_DIR"] = self.previous_memory_dir
        wiki_utils._META_DIR_CACHE = None
        if self._private_temp_root is not None:
            self._private_temp_root.cleanup()

    def _replace_claims(self, claims):
        proposed_claims = list(claims.get("items", {}).values())
        current_claims = governance_store.load_claims().get("items", {}).values()
        affected_pages = set()
        for claim in [*current_claims, *proposed_claims]:
            source_page = str(claim.get("source_page") or "").strip()
            locator = claim.setdefault("locator", {})
            page_key = str(locator.get("page_key") or "").strip()
            if not page_key and source_page:
                page_key = Path(source_page).stem
                locator["page_key"] = page_key
            if page_key:
                affected_pages.add(f"{page_key}.md")
        governance_store.apply_change_set(
            {
                "affected_pages": sorted(affected_pages),
                "proposed_entities": [],
                "proposed_claims": proposed_claims,
                "proposed_evidence": [],
                "proposed_source_updates": [],
                "proposed_edges": [],
            }
        )

    def _seed_claims(self):
        claims = governance_store.load_claims()
        claims["items"] = {
            "claim_old": {
                "claim_id": "claim_old",
                "claim_text": "Preferred deployment target: Legacy VM",
                "claim_type": "assertion",
                "memory_type": "preference",
                "memory_key": "deployment_target",
                "status": "Active",
                "confidence": 0.78,
                "authority_score": 0.6,
                "importance_score": 0.7,
                "evidence_ids": ["ev_old"],
                "source_ids": ["src_old"],
                "source_page": "Entity_Runtime.md",
                "updated_at": "2025-01-01T00:00:00+00:00",
                "created_at": "2025-01-01T00:00:00+00:00",
                "locator": {"heading": "Runtime"},
            },
            "claim_new": {
                "claim_id": "claim_new",
                "claim_text": "Preferred deployment target: Kubernetes",
                "claim_type": "assertion",
                "memory_type": "preference",
                "memory_key": "deployment_target",
                "status": "Active",
                "confidence": 0.74,
                "authority_score": 0.6,
                "importance_score": 0.7,
                "evidence_ids": ["ev_new"],
                "source_ids": ["src_new"],
                "source_page": "Entity_Runtime.md",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "created_at": "2026-01-01T00:00:00+00:00",
                "locator": {"heading": "Runtime"},
            },
            "claim_fact": {
                "claim_id": "claim_fact",
                "claim_text": "Agent memory should retrieve facts before loading full Markdown pages.",
                "claim_type": "assertion",
                "status": "Active",
                "confidence": 0.82,
                "evidence_ids": ["ev_fact"],
                "source_ids": ["src_fact"],
                "source_page": "Concept_Agent_Memory.md",
                "updated_at": "2026-01-02T00:00:00+00:00",
                "created_at": "2026-01-02T00:00:00+00:00",
                "locator": {"heading": "Agent Memory"},
            },
        }
        self._replace_claims(claims)

    def test_rebuild_operational_memory_resolves_typed_conflict(self):
        self._seed_claims()
        store = governance_store.rebuild_operational_memory()
        preferences = [
            item for item in store["items"].values()
            if item["memory_type"] == "preference"
        ]

        self.assertEqual(len(preferences), 2)
        old_item = next(item for item in preferences if "Legacy VM" in item["text"])
        new_item = next(item for item in preferences if "Kubernetes" in item["text"])
        self.assertEqual(old_item["validity_state"], "superseded")
        self.assertEqual(old_item["superseded_by"], new_item["memory_id"])
        self.assertEqual(new_item["conflict_resolution"]["state"], "winner")

    def test_memory_search_and_packet_prefer_runtime_memory(self):
        self._seed_claims()
        governance_store.rebuild_operational_memory()

        output = search_vector_lake("deployment target", mode="memory", top_k=3)
        self.assertIn("Kubernetes", output)
        self.assertNotIn("No operational memory", output)

        packet = build_memory_packet("deployment target", max_chars=12000)["packet"]
        self.assertIn("Current Preferences", packet)
        self.assertIn("Kubernetes", packet)
        self.assertIn("Conflicts / Stale Warnings", packet)
        self.assertIn("Legacy VM", packet)

    def test_memory_packet_uses_one_ranked_candidate_scan(self):
        self._seed_claims()
        governance_store.rebuild_operational_memory()

        with mock.patch.object(
            governance_store,
            "search_operational_memory_views",
            wraps=governance_store.search_operational_memory_views,
        ) as search_views:
            packet = build_memory_packet("deployment target", max_chars=12000)

        self.assertEqual(search_views.call_count, 1)
        self.assertIn("Kubernetes", packet["packet"])
        self.assertIn("Legacy VM", packet["packet"])

    def test_memory_search_only_decodes_ranked_candidates(self):
        store = governance_store.load_memory_objects()
        store["items"] = {
            f"noise_{index}": {
                "memory_id": f"noise_{index}",
                "memory_type": "fact",
                "memory_key": f"unrelated_{index}",
                "text": "Unrelated operational record.",
                "source_page": "Concept_Other.md",
                "validity_state": "active",
                "memory_score": 0.9,
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
            for index in range(500)
        }
        store["items"].update({
            "matching_active": {
                "memory_id": "matching_active",
                "memory_type": "fact",
                "memory_key": "needle_target",
                "text": "Needle target is active.",
                "source_page": "Concept_Target.md",
                "validity_state": "active",
                "memory_score": 0.8,
                "updated_at": "2026-01-02T00:00:00+00:00",
            },
            "matching_old": {
                "memory_id": "matching_old",
                "memory_type": "fact",
                "memory_key": "needle_target_history",
                "text": "Needle target was superseded.",
                "source_page": "Concept_Target.md",
                "validity_state": "superseded",
                "memory_score": 0.7,
                "updated_at": "2026-01-01T00:00:00+00:00",
            },
        })
        governance_store.save_memory_objects(store)

        with mock.patch.object(
            governance_store.json,
            "loads",
            wraps=governance_store.json.loads,
        ) as loads:
            current, history = governance_store.search_operational_memory_views(
                "needle target", current_top_k=4, history_top_k=4
            )
        legacy_current, legacy_history = (
            governance_store._legacy_operational_memory_views(
                "needle target", 4, 4, None, False
            )
        )

        self.assertEqual([item["memory_id"] for item in current], ["matching_active"])
        self.assertEqual(
            [item["memory_id"] for item in history],
            ["matching_active", "matching_old"],
        )
        self.assertEqual(current, legacy_current)
        self.assertEqual(history, legacy_history)
        self.assertLessEqual(loads.call_count, 4)

    def test_deleting_conflict_winner_reactivates_remaining_preference(self):
        self._seed_claims()
        claims = governance_store.load_claims()
        governance_store.rebuild_operational_memory()
        remaining = claims["items"]["claim_old"]

        governance_store.apply_change_sets_batch([{
            "affected_pages": ["Entity_Runtime.md"],
            "proposed_entities": [],
            "proposed_claims": [remaining],
            "proposed_evidence": [],
            "proposed_source_updates": [],
            "proposed_edges": [],
        }])

        memories = governance_store.load_memory_objects()["items"].values()
        preference = next(item for item in memories if item.get("source_claim_id") == "claim_old")
        self.assertEqual(preference["validity_state"], "active")
        self.assertNotIn("superseded_by", preference)
        self.assertFalse(any(item.get("source_claim_id") == "claim_new" for item in memories))

    def test_search_excludes_template_artifacts_and_audit_reports_them(self):
        store = governance_store.load_memory_objects()
        store["items"] = {
            "memory_stub": {
                "memory_id": "memory_stub",
                "memory_type": "fact",
                "memory_key": "generated_stub",
                "text": "This is an auto-generated stub page to prevent broken links from Concept_X.",
                "source_page": "Concept_X.md",
                "validity_state": "active",
                "memory_score": 1.0,
            },
            "memory_real": {
                "memory_id": "memory_real",
                "memory_type": "fact",
                "memory_key": "durable_fact",
                "text": "Concept X has a verified owner.",
                "source_page": "Concept_X.md",
                "validity_state": "active",
                "memory_score": 0.8,
            },
            "memory_mixed": {
                "memory_id": "memory_mixed",
                "memory_type": "fact",
                "memory_key": "mixed_page",
                "text": "[System Directive: latest consensus.] Concept X uses signed records.",
                "source_page": "Concept_X.md",
                "validity_state": "active",
                "memory_score": 0.7,
            },
        }
        governance_store.save_memory_objects(store)

        current = governance_store.search_operational_memory("Concept X", top_k=10)
        forensic = governance_store.search_operational_memory(
            "Concept X", top_k=10, include_polluted=True
        )
        preview = governance_store.remediate_operational_memory_pollution(dry_run=True)

        self.assertEqual(
            {item["memory_id"] for item in current}, {"memory_real", "memory_mixed"}
        )
        self.assertEqual(
            {item["memory_id"] for item in forensic},
            {"memory_real", "memory_mixed", "memory_stub"},
        )
        self.assertEqual(preview["candidate_count"], 1)
        self.assertEqual(preview["reason_counts"], {"generated_stub": 1})

    def test_rebuild_preserves_archived_artifact_without_reactivating_claim(self):
        claim = {
            "claim_id": "claim_legacy_stub",
            "claim_text": "This is an auto-generated stub page to prevent broken links from Concept_X.",
            "status": "Active",
            "source_page": "Concept_X.md",
            "evidence_ids": [],
            "source_ids": [],
        }
        claims = governance_store.load_claims()
        claims["items"] = {claim["claim_id"]: claim}
        self._replace_claims(claims)
        legacy = {
            "memory_id": "memory_legacy_stub",
            "memory_type": "fact",
            "text": claim["claim_text"],
            "source_claim_id": claim["claim_id"],
            "source_page": claim["source_page"],
            "validity_state": "archived",
            "validity_reasons": ["infrastructure_artifact:generated_stub"],
            "memory_score": 0.0,
        }
        store = governance_store.load_memory_objects()
        store["items"] = {legacy["memory_id"]: legacy}
        governance_store.save_memory_objects(store)

        rebuilt = governance_store.rebuild_operational_memory()

        self.assertEqual(set(rebuilt["items"]), {"memory_legacy_stub"})
        self.assertEqual(
            rebuilt["items"]["memory_legacy_stub"]["validity_state"], "archived"
        )
        self.assertEqual(
            governance_store.remediate_operational_memory_pollution(dry_run=True)[
                "candidate_count"
            ],
            0,
        )

    def test_page_delta_preserves_archived_infrastructure_history(self):
        claim = {
            "claim_id": "claim_delta_stub",
            "claim_text": "This is an auto-generated stub page to prevent broken links from Concept_Y.",
            "status": "Active",
            "source_page": "Concept_Delta.md",
            "evidence_ids": [],
            "source_ids": [],
            "locator": {"page_key": "Concept_Delta", "heading": "Mechanism"},
        }
        claims = governance_store.load_claims()
        claims["items"] = {claim["claim_id"]: claim}
        self._replace_claims(claims)
        memory = governance_store._memory_object_from_claim(claim)
        memory.update({
            "validity_state": "archived",
            "validity_reasons": ["infrastructure_artifact:generated_stub"],
            "memory_score": 0.0,
        })
        store = governance_store.load_memory_objects()
        store["items"] = {memory["memory_id"]: memory}
        governance_store.save_memory_objects(store)

        governance_store.apply_change_sets_batch([{
            "affected_pages": ["Concept_Delta.md"],
            "proposed_entities": [],
            "proposed_claims": [],
            "proposed_evidence": [],
            "proposed_source_updates": [],
            "proposed_edges": [],
        }])

        memories = governance_store.load_memory_objects()["items"].values()
        archived = [
            item for item in memories
            if item.get("source_claim_id") == "claim_delta_stub"
        ]
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["validity_state"], "archived")

    def test_single_typed_memory_keeps_unsupported_validity(self):
        memory = {
            "memory_id": "memory_unsupported_preference",
            "memory_type": "preference",
            "memory_key": "deployment_target",
            "validity_state": "unsupported",
            "memory_score": 0.2,
        }
        store = {"items": {memory["memory_id"]: memory}}

        governance_store._resolve_memory_conflicts(store)

        self.assertEqual(memory["validity_state"], "unsupported")


    def test_candidate_prefilter_keeps_the_hundredth_query_term(self):
        query = " ".join(f"term{index:03d}" for index in range(100))
        memory = {
            "memory_id": "late_term_match",
            "memory_type": "fact",
            "memory_key": "late_term",
            "text": "Only term099 identifies this record.",
            "source_page": "Concept_Late-Term.md",
            "validity_state": "active",
            "memory_score": 0.8,
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        governance_store.save_memory_objects(
            {"items": {memory["memory_id"]: memory}}
        )

        current, history = governance_store.search_operational_memory_views(
            query,
            current_top_k=4,
            history_top_k=4,
        )
        legacy_current, legacy_history = (
            governance_store._legacy_operational_memory_views(
                query,
                4,
                4,
                None,
                False,
            )
        )

        self.assertEqual(current, legacy_current)
        self.assertEqual(history, legacy_history)
        self.assertEqual([item["memory_id"] for item in current], ["late_term_match"])


    def test_candidate_prefilter_keeps_long_cjk_tail_terms(self):
        query = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申"
        memory = {
            "memory_id": "cjk_tail_match",
            "memory_type": "fact",
            "memory_key": "cjk_tail",
            "text": "仅有未申可识别这条记录。",
            "source_page": "Concept_CJK-Tail.md",
            "validity_state": "active",
            "memory_score": 0.8,
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        governance_store.save_memory_objects(
            {"items": {memory["memory_id"]: memory}}
        )

        current, history = governance_store.search_operational_memory_views(
            query,
            current_top_k=4,
            history_top_k=4,
        )
        legacy_current, legacy_history = (
            governance_store._legacy_operational_memory_views(
                query,
                4,
                4,
                None,
                False,
            )
        )

        self.assertEqual(current, legacy_current)
        self.assertEqual(history, legacy_history)
        self.assertEqual([item["memory_id"] for item in current], ["cjk_tail_match"])


def _seed_isolated_search_memories(records):
    governance_store.initialize_meta_store()
    governance_store.save_memory_objects({
        "items": {record["memory_id"]: record for record in records}
    })
    return db_store.get_connection()


def _reset_search_index_as_legacy(conn):
    with db_store.transaction():
        conn.execute("DELETE FROM operational_memory_search_fts")
        conn.execute("DELETE FROM operational_memory_search_short_fts")
        conn.execute("DELETE FROM operational_memory_search_docs")
        conn.execute("DELETE FROM operational_memory_search_pending")
        conn.execute(
            "UPDATE operational_memory_search_state SET "
            "backfill_cursor = '', "
            "backfill_target = (SELECT COALESCE(MAX(memory_id), '') "
            "FROM operational_memory) WHERE singleton = 1"
        )


def test_lazy_fts_backfill_preserves_exact_results(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    records = [
        {
            "memory_id": f"memory_{index}",
            "memory_type": "fact",
            "memory_key": f"key_{index}",
            "text": text,
            "source_page": "Concept_Search.md",
            "validity_state": "active",
            "memory_score": 0.5 + (index / 100),
            "updated_at": f"2026-01-0{index + 1}T00:00:00+00:00",
        }
        for index, text in enumerate([
            "needle alpha",
            "unrelated beta",
            "needle gamma",
            "仅有甲可匹配",
            "needle delta",
            "unrelated epsilon",
        ])
    ]
    conn = _seed_isolated_search_memories(records)
    _reset_search_index_as_legacy(conn)
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_INDEX_BATCH", "2")
    initial_status = governance_store.operational_memory_search_index_status()
    assert initial_status["ready"] is False
    assert "operational_memory_search_backfill_incomplete" in initial_status["warnings"]

    expected = governance_store._legacy_operational_memory_views(
        "needle", 10, 10, None, False
    )
    actual = governance_store.search_operational_memory_views(
        "needle", current_top_k=10, history_top_k=10
    )

    assert actual == expected
    state = conn.execute(
        "SELECT backfill_cursor, backfill_target "
        "FROM operational_memory_search_state"
    ).fetchone()
    assert state[0] == "" < state[1]
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_docs"
    ).fetchone()[0] == 0

    progress = governance_store.maintain_operational_memory_search_index(2)
    assert progress["backfill_cursor"] == "memory_1"
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_docs"
    ).fetchone()[0] == 2
    conn.execute("VACUUM")
    for _ in range(3):
        governance_store.maintain_operational_memory_search_index(2)
    state = conn.execute(
        "SELECT backfill_cursor, backfill_target "
        "FROM operational_memory_search_state"
    ).fetchone()
    assert state[0] == state[1]
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_docs"
    ).fetchone()[0] == len(records)
    ready_status = governance_store.operational_memory_search_index_status()
    assert ready_status["ready"] is True
    assert ready_status["schema_version"] == 7
    assert ready_status["warnings"] == []

    with mock.patch.object(
        governance_store,
        "_operational_memory_candidate_sql",
        side_effect=AssertionError("ready FTS must not use the O(N) prefilter"),
    ):
        indexed = governance_store.search_operational_memory("needle", top_k=10)
    assert {item["memory_id"] for item in indexed} == {
        "memory_0",
        "memory_2",
        "memory_4",
    }

    with mock.patch.object(
        governance_store,
        "_advance_operational_memory_search_index",
        side_effect=AssertionError("search must never advance the derived index"),
    ):
        cjk = governance_store.search_operational_memory("甲", top_k=10)
    assert [item["memory_id"] for item in cjk] == ["memory_3"]


def test_ready_dual_fts_matches_legacy_cjk_and_has_no_canonical_scan(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    records = [
        {
            "memory_id": "memory_cjk_single",
            "memory_type": "fact",
            "memory_key": "甲状腺",
            "text": "甲型临床路径",
            "source_page": "Concept_CJK.md",
            "validity_state": "active",
            "memory_score": 0.7,
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "memory_id": "memory_cjk_pair",
            "memory_type": "fact",
            "memory_key": "医疗信息化",
            "text": "医疗信息系统互操作",
            "source_page": "Concept_CJK.md",
            "validity_state": "active",
            "memory_score": 0.8,
            "updated_at": "2026-01-02T00:00:00+00:00",
        },
        {
            "memory_id": "memory_cjk_mixed",
            "memory_type": "fact",
            "memory_key": "医疗ai",
            "text": "医疗AI辅助诊断",
            "source_page": "Concept_CJK.md",
            "validity_state": "active",
            "memory_score": 0.9,
            "updated_at": "2026-01-03T00:00:00+00:00",
        },
        {
            "memory_id": "memory_unrelated",
            "memory_type": "fact",
            "memory_key": "unrelated",
            "text": "普通运营记录",
            "source_page": "Concept_Other.md",
            "validity_state": "active",
            "memory_score": 0.6,
            "updated_at": "2026-01-04T00:00:00+00:00",
        },
        {
            "memory_id": "memory_unicode_extensions",
            "memory_type": "fact",
            "memory_key": "𠀀型élan",
            "text": "扩展汉字𠀀与Unicode字母é、α",
            "source_page": "Concept_Unicode.md",
            "validity_state": "active",
            "memory_score": 0.75,
            "updated_at": "2026-01-05T00:00:00+00:00",
        },
    ]
    conn = _seed_isolated_search_memories(records)
    assert governance_store.maintain_operational_memory_search_index(100)["ready"]
    state = conn.execute(
        "SELECT backfill_cursor, backfill_target FROM operational_memory_search_state"
    ).fetchone()

    for query in ("甲", "医疗", "AI", "医疗AI", "𠀀", "é", "α"):
        expected = governance_store._legacy_operational_memory_views(
            query, 10, 10, None, False
        )
        actual = governance_store.search_operational_memory_views(
            query, current_top_k=10, history_top_k=10
        )
        assert actual == expected

        sql, params = governance_store._indexed_operational_memory_query(
            governance_store._bounded_memory_query_terms(query),
            None,
            cursor=str(state[0]),
            target=str(state[1]),
            include_pending=False,
        )
        assert "instr(" not in sql.casefold()
        plan = [
            str(row[3]).casefold()
            for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
        ]
        assert not any(
            detail == "scan operational_memory"
            or detail.startswith("scan operational_memory ")
            for detail in plan
        )
        assert any("virtual table index" in detail for detail in plan)


def test_equal_rank_results_use_stable_memory_id_after_physical_reorder(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "0")
    records = [
        {
            "memory_id": memory_id,
            "memory_type": "fact",
            "memory_key": "equal rank",
            "text": "stable tie",
            "source_page": "Concept_Stable-Tie.md",
            "validity_state": "active",
            "memory_score": 0.8,
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        for memory_id in ("memory-z", "memory-a")
    ]
    conn = _seed_isolated_search_memories(records)

    def result_ids():
        return [
            item["memory_id"]
            for item in governance_store.search_operational_memory(
                "stable tie",
                top_k=2,
            )
        ]

    assert result_ids() == ["memory-a", "memory-z"]
    stored = conn.execute(
        "SELECT memory_id, memory_type, data_json, updated_at "
        "FROM operational_memory WHERE memory_id = 'memory-a'"
    ).fetchone()
    with db_store.transaction():
        conn.execute("DELETE FROM operational_memory WHERE memory_id = 'memory-a'")
        conn.execute(
            "INSERT INTO operational_memory "
            "(memory_id, memory_type, data_json, updated_at) VALUES (?, ?, ?, ?)",
            tuple(stored),
        )
    conn.execute("VACUUM")

    assert result_ids() == ["memory-a", "memory-z"]
    legacy, _ = governance_store._legacy_operational_memory_views(
        "stable tie", 2, 0, None, False
    )
    assert [item["memory_id"] for item in legacy] == ["memory-a", "memory-z"]


def test_pending_rows_keep_update_and_delete_results_exact(
    isolated_memory,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    record = {
        "memory_id": "memory_mutable",
        "memory_type": "fact",
        "memory_key": "mutable_key",
        "text": "old search phrase",
        "source_page": "Concept_Mutable.md",
        "validity_state": "active",
        "memory_score": 0.8,
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    conn = _seed_isolated_search_memories([record])
    governance_store.maintain_operational_memory_search_index(10)

    changed = dict(record)
    changed["text"] = "new search phrase"
    changed["updated_at"] = "2026-01-02T00:00:00+00:00"
    with db_store.transaction():
        conn.execute(
            "UPDATE operational_memory SET data_json = ?, updated_at = ? "
            "WHERE memory_id = ?",
            (
                governance_store.json.dumps(changed, ensure_ascii=False),
                changed["updated_at"],
                changed["memory_id"],
            ),
        )

    assert [
        item["memory_id"]
        for item in governance_store.search_operational_memory("new", top_k=10)
    ] == ["memory_mutable"]
    assert governance_store.search_operational_memory("old", top_k=10) == []

    with db_store.transaction():
        conn.execute(
            "DELETE FROM operational_memory WHERE memory_id = ?",
            (changed["memory_id"],),
        )
    assert governance_store.search_operational_memory("new", top_k=10) == []

    governance_store.maintain_operational_memory_search_index(10)
    assert governance_store.search_operational_memory("new", top_k=10) == []
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_pending"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_docs"
    ).fetchone()[0] == 0


def test_ready_short_fts_update_removes_stale_tokens_without_canonical_scan(
    isolated_memory,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    record = {
        "memory_id": "memory_cjk_update",
        "memory_type": "fact",
        "memory_key": "cjk_update",
        "text": "甲型临床路径",
        "source_page": "Concept_CJK-Update.md",
        "validity_state": "active",
        "memory_score": 0.8,
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    conn = _seed_isolated_search_memories([record])
    assert governance_store.maintain_operational_memory_search_index(10)["ready"]

    changed = dict(record)
    changed["text"] = "乙型临床路径"
    changed["updated_at"] = "2026-01-02T00:00:00+00:00"
    with db_store.transaction():
        conn.execute(
            "UPDATE operational_memory SET data_json = ?, updated_at = ? "
            "WHERE memory_id = ?",
            (
                governance_store.json.dumps(changed, ensure_ascii=False),
                changed["updated_at"],
                changed["memory_id"],
            ),
        )
    assert governance_store.maintain_operational_memory_search_index(10)["ready"]

    stale_rows = conn.execute(
        "SELECT rowid FROM operational_memory_search_short_fts "
        "WHERE operational_memory_search_short_fts MATCH ?",
        ('"甲"',),
    ).fetchall()
    current_rows = conn.execute(
        "SELECT rowid FROM operational_memory_search_short_fts "
        "WHERE operational_memory_search_short_fts MATCH ?",
        ('"乙"',),
    ).fetchall()
    assert stale_rows == []
    assert len(current_rows) == 1

    state = conn.execute(
        "SELECT backfill_cursor, backfill_target "
        "FROM operational_memory_search_state WHERE singleton = 1"
    ).fetchone()
    sql, params = governance_store._indexed_operational_memory_query(
        ["乙"],
        None,
        cursor=str(state[0]),
        target=str(state[1]),
        include_pending=False,
    )
    assert "instr(" not in sql.casefold()
    plan = [
        str(row[3]).casefold()
        for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
    ]
    assert not any(
        detail == "scan operational_memory"
        or detail.startswith("scan operational_memory ")
        for detail in plan
    )
    assert governance_store.search_operational_memory("甲", top_k=10) == []
    assert [
        item["memory_id"]
        for item in governance_store.search_operational_memory("乙", top_k=10)
    ] == ["memory_cjk_update"]



def test_search_falls_back_quickly_when_lazy_index_write_is_locked(
    isolated_memory,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    record = {
        "memory_id": "memory_locked",
        "memory_type": "fact",
        "memory_key": "locked_key",
        "text": "locked fallback remains readable",
        "source_page": "Concept_Locked.md",
        "validity_state": "active",
        "memory_score": 0.8,
    }
    conn = _seed_isolated_search_memories([record])
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_INDEX_BATCH", "10")
    blocker = sqlite3.connect(str(db_store.get_db_path()), timeout=0.1)
    blocker.execute("PRAGMA journal_mode=WAL")
    blocker.execute("BEGIN IMMEDIATE")
    try:
        started = time.perf_counter()
        result = governance_store.search_operational_memory("locked", top_k=10)
        elapsed = time.perf_counter() - started
    finally:
        blocker.rollback()
        blocker.close()

    assert [item["memory_id"] for item in result] == ["memory_locked"]
    assert elapsed < 1.0
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_pending"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_docs"
    ).fetchone()[0] == 0

    result = governance_store.search_operational_memory("locked", top_k=10)
    assert [item["memory_id"] for item in result] == ["memory_locked"]
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_pending"
    ).fetchone()[0] == 1
    governance_store.maintain_operational_memory_search_index(10)
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_pending"
    ).fetchone()[0] == 0



def test_disabling_fts_removes_triggers_without_reclaiming_derived_tables(
    isolated_memory,
    monkeypatch,
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    record = {
        "memory_id": "memory_disable",
        "memory_type": "fact",
        "memory_key": "disable_key",
        "text": "disable the optional index",
        "source_page": "Concept_Disable.md",
        "validity_state": "active",
        "memory_score": 0.8,
    }
    conn = _seed_isolated_search_memories([record])
    governance_store.maintain_operational_memory_search_index(10)
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_docs"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_pending"
    ).fetchone()[0] == 0

    db_path = db_store.get_db_path().resolve()
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "0")
    db_store.close_all_connections()
    db_store._INITIALIZED_DB_PATHS.discard(str(db_path))
    db_store.init_db()
    conn = db_store.get_connection()
    trigger_count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' "
        "AND name LIKE 'trg_operational_memory_search_%'"
    ).fetchone()[0]
    assert trigger_count == 0

    changed = dict(record)
    changed["text"] = "disabled updates must not queue derived work"
    with db_store.transaction():
        conn.execute(
            "UPDATE operational_memory SET data_json = ? WHERE memory_id = ?",
            (
                governance_store.json.dumps(changed, ensure_ascii=False),
                changed["memory_id"],
            ),
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_pending"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_docs"
    ).fetchone()[0] == 1


def test_memory_search_index_tool_is_preview_first(
    isolated_memory,
    monkeypatch,
):
    from vector_lake import tools

    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    record = {
        "memory_id": "memory_tool",
        "memory_type": "fact",
        "memory_key": "tool_key",
        "text": "explicit maintenance tool",
        "source_page": "Concept_Tool.md",
        "validity_state": "active",
        "memory_score": 0.8,
    }
    conn = _seed_isolated_search_memories([record])

    preview = governance_store.json.loads(
        tools.operational_memory_search_index_maintenance()
    )
    assert preview["dry_run"] is True
    assert preview["before"]["pending"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_docs"
    ).fetchone()[0] == 0

    applied = governance_store.json.loads(
        tools.operational_memory_search_index_maintenance(
            dry_run=False,
            batch_size=1,
        )
    )
    assert applied["dry_run"] is False
    assert applied["after"]["ready"] is True
    assert applied["after"]["indexed_documents"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_pending"
    ).fetchone()[0] == 0

def test_search_index_v5_to_v7_upgrade_resets_only_derived_state(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    record = {
        "memory_id": "memory_upgrade",
        "memory_type": "fact",
        "memory_key": "upgrade_key",
        "text": "upgrade search index safely",
        "source_page": "Concept_Upgrade.md",
        "validity_state": "active",
        "memory_score": 0.8,
    }
    conn = _seed_isolated_search_memories([record])
    governance_store.maintain_operational_memory_search_index(10)
    with db_store.transaction():
        conn.execute(
            "UPDATE operational_memory_search_state SET schema_version = 5"
        )
        assert db_store._init_operational_memory_search_schema(conn) is True

    state = conn.execute(
        "SELECT backfill_cursor, backfill_target, schema_version "
        "FROM operational_memory_search_state"
    ).fetchone()
    assert tuple(state) == ("", "memory_upgrade", 7)
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_docs"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_fts"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_short_fts"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 1
    assert [
        item["memory_id"]
        for item in governance_store.search_operational_memory("upgrade", top_k=10)
    ] == ["memory_upgrade"]


def test_search_index_v6_to_v7_adds_revision_without_replaying_derived_state(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    record = {
        "memory_id": "memory_v6_upgrade",
        "memory_type": "fact",
        "memory_key": "v6_upgrade_key",
        "text": "preserve the certified derived projection",
        "source_page": "Concept_Upgrade.md",
        "validity_state": "active",
        "memory_score": 0.8,
    }
    conn = _seed_isolated_search_memories([record])
    assert governance_store.maintain_operational_memory_search_index(10)["ready"]
    before = tuple(
        conn.execute(
            "SELECT backfill_cursor, backfill_target, proof_status, "
            "proof_generation FROM operational_memory_search_state"
        ).fetchone()
    )
    before_counts = tuple(
        conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM operational_memory_search_docs), "
            "(SELECT COUNT(*) FROM operational_memory_search_fts), "
            "(SELECT COUNT(*) FROM operational_memory_search_short_fts)"
        ).fetchone()
    )

    with db_store.transaction():
        for trigger_name in (
            "trg_operational_memory_search_docs_insert_revision",
            "trg_operational_memory_search_docs_update_revision",
            "trg_operational_memory_search_docs_delete_revision",
            "trg_operational_memory_search_pending_insert_revision",
            "trg_operational_memory_search_pending_update_revision",
            "trg_operational_memory_search_pending_delete_revision",
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
        conn.execute("DROP TABLE operational_memory_search_revision")
        conn.execute(
            "UPDATE operational_memory_search_state SET schema_version = 6"
        )
        assert db_store._init_operational_memory_search_schema(conn) is True

    after = tuple(
        conn.execute(
            "SELECT backfill_cursor, backfill_target, proof_status, "
            "proof_generation FROM operational_memory_search_state"
        ).fetchone()
    )
    after_counts = tuple(
        conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM operational_memory_search_docs), "
            "(SELECT COUNT(*) FROM operational_memory_search_fts), "
            "(SELECT COUNT(*) FROM operational_memory_search_short_fts)"
        ).fetchone()
    )
    assert after == before
    assert after_counts == before_counts == (1, 1, 1)
    assert conn.execute(
        "SELECT schema_version FROM operational_memory_search_state"
    ).fetchone()[0] == 7
    assert conn.execute(
        "SELECT revision FROM operational_memory_search_revision"
    ).fetchone()[0] == 0
    assert governance_store.operational_memory_search_index_status()["ready"]


@pytest.mark.parametrize("legacy_schema_version", [3, 4, 5])
def test_search_index_v7_rebuilds_legacy_integer_cursor_affinity(
    isolated_memory, monkeypatch, legacy_schema_version
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    records = [
        {
            "memory_id": memory_id,
            "memory_type": "fact",
            "memory_key": memory_id,
            "text": "stable keyset",
            "source_page": "Concept_Keyset.md",
            "validity_state": "active",
            "memory_score": 0.8,
        }
        for memory_id in ("001", "010")
    ]
    conn = _seed_isolated_search_memories(records)
    with db_store.transaction():
        conn.execute("DROP TABLE operational_memory_search_state")
        conn.execute(
            "CREATE TABLE operational_memory_search_state ("
            "singleton INTEGER PRIMARY KEY, "
            "backfill_cursor INTEGER NOT NULL DEFAULT 0, "
            "backfill_target INTEGER NOT NULL DEFAULT 0, "
            "schema_version INTEGER NOT NULL DEFAULT 4, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO operational_memory_search_state "
            "VALUES (1, 0, 10, ?, NULL)",
            (legacy_schema_version,),
        )
        assert db_store._init_operational_memory_search_schema(conn) is True

    column_types = {
        row[1]: row[2]
        for row in conn.execute(
            "PRAGMA table_info(operational_memory_search_state)"
        )
    }
    state = conn.execute(
        "SELECT backfill_cursor, backfill_target, schema_version "
        "FROM operational_memory_search_state"
    ).fetchone()

    assert column_types["backfill_cursor"] == "TEXT"
    assert column_types["backfill_target"] == "TEXT"
    assert tuple(state) == ("", "010", 7)
    progress = governance_store.maintain_operational_memory_search_index(1)
    assert progress["ready"] is False
    assert progress["backfill_cursor"] == "001"
    conn.execute("VACUUM")
    progress = governance_store.maintain_operational_memory_search_index(1)
    assert progress["ready"] is True
    assert progress["backfill_cursor"] == "010"
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_docs"
    ).fetchone()[0] == 2


def test_empty_search_index_is_ready_without_backfill(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    governance_store.initialize_meta_store()
    conn = db_store.get_connection()

    assert governance_store.search_operational_memory("anything", top_k=10) == []
    state = conn.execute(
        "SELECT backfill_cursor, backfill_target, schema_version "
        "FROM operational_memory_search_state"
    ).fetchone()
    assert tuple(state) == ("", "", 7)
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_docs"
    ).fetchone()[0] == 0


def test_operational_memory_cjk_search_scalable_benchmark(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_ATTESTATION_SECONDS", "60")
    row_count = max(
        1_000,
        int(os.environ.get("VECTOR_LAKE_OPERATIONAL_MEMORY_BENCHMARK_ROWS", "2000")),
    )
    governance_store.initialize_meta_store()
    conn = db_store.get_connection()
    for offset in range(0, row_count, 5_000):
        batch = []
        for index in range(offset, min(row_count, offset + 5_000)):
            memory_id = f"benchmark_{index:06d}"
            text = (
                "医疗AI benchmark needle"
                if index == row_count - 1
                else f"routine operational record {index}"
            )
            payload = {
                "memory_id": memory_id,
                "memory_type": "fact",
                "memory_key": f"benchmark_key_{index}",
                "text": text,
                "source_page": "Concept_Benchmark.md",
                "validity_state": "active",
                "memory_score": 0.8,
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
            batch.append((
                memory_id,
                "fact",
                0.8,
                "Active",
                365.0,
                governance_store.json.dumps(payload, ensure_ascii=False),
                payload["updated_at"],
            ))
        with db_store.transaction():
            conn.executemany(
                "INSERT INTO operational_memory "
                "(memory_id, memory_type, score, status, ttl, data_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                batch,
            )

    while True:
        progress = governance_store.maintain_operational_memory_search_index(10_000)
        if progress["ready"]:
            break

    real_inspect = db_store.inspect_operational_memory_search_integrity
    inspections = []

    def counted_inspect(*args, **kwargs):
        inspections.append(1)
        return real_inspect(*args, **kwargs)

    monkeypatch.setattr(
        db_store,
        "inspect_operational_memory_search_integrity",
        counted_inspect,
    )
    db_store.close_all_connections()
    started = time.perf_counter()
    cold = governance_store.search_operational_memory("医疗AI", top_k=10)
    cold_seconds = time.perf_counter() - started
    started = time.perf_counter()
    hot = governance_store.search_operational_memory("医疗AI", top_k=10)
    hot_seconds = time.perf_counter() - started

    assert [item["memory_id"] for item in cold] == [
        f"benchmark_{row_count - 1:06d}"
    ]
    assert hot == cold
    assert inspections == [1]
    assert cold_seconds < 2.0
    assert hot_seconds < 0.5

    _tamper_memory_fts_with_equal_counts(
        f"benchmark_{row_count - 1:06d}",
        text="equal count benchmark corruption",
    )
    db_store.get_connection()._operational_memory_search_integrity_cache[
        "attested_at_monotonic"
    ] -= 61
    repair_batch_size = 10_000
    expected_repair_batches = (row_count + repair_batch_size - 1) // repair_batch_size
    repair_started = time.perf_counter()
    repair_batches = 0
    while True:
        progress = governance_store.maintain_operational_memory_search_index(
            repair_batch_size
        )
        repair_batches += 1
        if progress["ready"]:
            break
        assert repair_batches < expected_repair_batches + 1
    repair_seconds = time.perf_counter() - repair_started
    assert repair_batches == expected_repair_batches
    assert repair_seconds < float(
        os.environ.get(
            "VECTOR_LAKE_OPERATIONAL_MEMORY_BENCHMARK_REPAIR_SECONDS",
            "60",
        )
    )
    assert [
        item["memory_id"]
        for item in governance_store.search_operational_memory("医疗AI", top_k=10)
    ] == [f"benchmark_{row_count - 1:06d}"]
    print(
        "operational-memory benchmark "
        f"rows={row_count} cold_proof={cold_seconds:.6f}s "
        f"hot={hot_seconds:.6f}s repair={repair_seconds:.6f}s "
        f"repair_batches={repair_batches}"
    )


def test_fts_can_be_disabled_without_changing_search_results(
    isolated_memory,
    monkeypatch,
):
    record = {
        "memory_id": "memory_fallback",
        "memory_type": "fact",
        "memory_key": "fallback_key",
        "text": "fallback remains exact",
        "source_page": "Concept_Fallback.md",
        "validity_state": "active",
        "memory_score": 0.8,
    }
    conn = _seed_isolated_search_memories([record])
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "0")

    with mock.patch.object(
        governance_store,
        "_operational_memory_candidate_sql",
        wraps=governance_store._operational_memory_candidate_sql,
    ) as compatibility_query:
        result = governance_store.search_operational_memory("fallback", top_k=10)

    assert [item["memory_id"] for item in result] == ["memory_fallback"]
    assert compatibility_query.call_count == 1
    schema_names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE name LIKE 'operational_memory_search_%'"
        )
    }
    assert "operational_memory_search_fts" not in schema_names
    assert "operational_memory_search_docs" not in schema_names


def test_configured_fts_missing_schema_warns_and_uses_compatibility_scan(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "0")
    record = {
        "memory_id": "memory_missing_schema",
        "memory_type": "fact",
        "memory_key": "missing_schema",
        "text": "兼容检索",
        "source_page": "Concept_Fallback.md",
        "validity_state": "active",
        "memory_score": 0.8,
    }
    _seed_isolated_search_memories([record])
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")

    status = governance_store.operational_memory_search_index_status()
    assert status["ready"] is False
    assert status["warnings"] == ["operational_memory_search_schema_unavailable"]
    assert [
        item["memory_id"]
        for item in governance_store.search_operational_memory("兼容", top_k=10)
    ] == ["memory_missing_schema"]


def test_legacy_v5_readonly_status_is_unavailable_and_large_search_fails_closed(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_DEGRADED_ROW_LIMIT", "1")
    conn = _seed_isolated_search_memories(_integrity_test_records(count=2))
    assert governance_store.maintain_operational_memory_search_index(10)["ready"]
    with db_store.transaction():
        conn.execute("DROP TABLE operational_memory_search_state")
        conn.execute(
            "CREATE TABLE operational_memory_search_state ("
            "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
            "backfill_cursor TEXT NOT NULL DEFAULT '', "
            "backfill_target TEXT NOT NULL DEFAULT '', "
            "schema_version INTEGER NOT NULL DEFAULT 5, updated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO operational_memory_search_state "
            "VALUES (1, 'memory_1', 'memory_1', 5, NULL)"
        )
    conn.execute("PRAGMA query_only=ON")
    try:
        status = governance_store.operational_memory_search_index_status(
            connection=conn
        )
        assert status["available"] is False
        assert status["ready"] is False
        assert status["warnings"] == [
            "operational_memory_search_schema_unavailable"
        ]
        with pytest.raises(governance_store.OperationalMemoryNotReady) as error:
            governance_store.search_operational_memory("needle", top_k=10)
        assert error.value.reason == "search_index_unavailable"
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(operational_memory_search_state)"
            )
        }
        assert "proof_status" not in columns
    finally:
        conn.execute("PRAGMA query_only=OFF")


def test_unready_large_search_fails_closed_before_canonical_scan(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_DEGRADED_ROW_LIMIT", "3")
    records = [
        {
            "memory_id": f"memory_{index}",
            "memory_type": "fact",
            "memory_key": f"key_{index}",
            "text": "rare needle" if index == 3 else "unrelated",
            "source_page": "Concept_Bounded.md",
            "validity_state": "active",
            "memory_score": 0.8,
        }
        for index in range(4)
    ]
    conn = _seed_isolated_search_memories(records)
    _reset_search_index_as_legacy(conn)

    with mock.patch.object(
        governance_store,
        "_indexed_operational_memory_query",
        side_effect=AssertionError("large degraded source must not be scanned"),
    ):
        with pytest.raises(governance_store.OperationalMemoryNotReady) as error:
            governance_store.search_operational_memory("needle", top_k=10)

    assert error.value.reason == "search_index_backfilling"
    assert error.value.retry_after_seconds == 5


def test_bounded_auto_maintenance_converges_to_ready(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    records = [
        {
            "memory_id": f"memory_{index}",
            "memory_type": "fact",
            "memory_key": f"key_{index}",
            "text": "bounded maintainer",
            "source_page": "Concept_Maintainer.md",
            "validity_state": "active",
            "memory_score": 0.8,
        }
        for index in range(6)
    ]
    conn = _seed_isolated_search_memories(records)
    _reset_search_index_as_legacy(conn)

    result = governance_store.maintain_operational_memory_search_index_budget(
        batch_size=2,
        max_batches=4,
        wall_seconds=2.0,
    )

    assert result["ready"] is True
    assert result["batches"] == 3
    assert result["canonical_documents"] == 6
    assert result["indexed_documents"] == 6


def test_ready_auto_maintenance_budget_is_zero_work(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    conn = _seed_isolated_search_memories(_integrity_test_records())
    assert governance_store.maintain_operational_memory_search_index(100)["ready"]
    state_before = tuple(conn.execute(
        "SELECT proof_generation, updated_at "
        "FROM operational_memory_search_state WHERE singleton = 1"
    ).fetchone())
    total_changes_before = conn.total_changes

    def forbid_advance(*_args, **_kwargs):
        raise AssertionError("ready maintenance must not open a repair transaction")

    monkeypatch.setattr(
        governance_store,
        "_advance_operational_memory_search_index",
        forbid_advance,
    )
    result = governance_store.maintain_operational_memory_search_index_budget(
        batch_size=2,
        max_batches=4,
        wall_seconds=2.0,
    )

    assert result["ready"] is True
    assert result["batches"] == 0
    assert conn.total_changes == total_changes_before
    assert tuple(conn.execute(
        "SELECT proof_generation, updated_at "
        "FROM operational_memory_search_state WHERE singleton = 1"
    ).fetchone()) == state_before


def test_ready_index_corruption_fails_closed_and_bounded_maintenance_repairs(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_DEGRADED_ROW_LIMIT", "3")
    records = [
        {
            "memory_id": f"memory_{index}",
            "memory_type": "fact",
            "memory_key": f"key_{index}",
            "text": "rare repair needle" if index == 3 else "unrelated",
            "source_page": "Concept_Repair.md",
            "validity_state": "active",
            "memory_score": 0.8,
        }
        for index in range(4)
    ]
    conn = _seed_isolated_search_memories(records)
    assert governance_store.maintain_operational_memory_search_index(100)["ready"]

    with db_store.transaction():
        governance_store._delete_memory_search_documents(conn, ["memory_3"])
        conn.execute("DELETE FROM operational_memory_search_pending")

    status = governance_store.operational_memory_search_index_status()
    assert status["ready"] is False
    assert any(
        warning.startswith("operational_memory_search_document_count_mismatch:")
        for warning in status["warnings"]
    )
    with mock.patch.object(
        governance_store,
        "_operational_memory_candidate_sql",
        side_effect=AssertionError("large corrupt index must fail before scan"),
    ):
        with pytest.raises(governance_store.OperationalMemoryNotReady) as error:
            governance_store.search_operational_memory("needle", top_k=10)
    assert error.value.reason == "search_index_integrity_mismatch"

    repaired = governance_store.maintain_operational_memory_search_index_budget(
        batch_size=2,
        max_batches=3,
        wall_seconds=2.0,
    )
    assert repaired["ready"] is True
    assert repaired["canonical_documents"] == 4
    assert repaired["indexed_documents"] == 4
    assert [
        item["memory_id"]
        for item in governance_store.search_operational_memory("needle", top_k=10)
    ] == ["memory_3"]

    doc_id = int(conn.execute(
        "SELECT doc_id FROM operational_memory_search_docs WHERE memory_id = ?",
        ("memory_3",),
    ).fetchone()[0])
    with db_store.transaction():
        conn.execute(
            "DELETE FROM operational_memory_search_fts WHERE rowid = ?",
            (doc_id,),
        )
    assert governance_store.operational_memory_search_index_status()["ready"] is False
    with pytest.raises(governance_store.OperationalMemoryNotReady) as error:
        governance_store.search_operational_memory("needle", top_k=10)
    assert error.value.reason == "search_index_integrity_mismatch"
    repaired = governance_store.maintain_operational_memory_search_index_budget(
        batch_size=2,
        max_batches=3,
        wall_seconds=2.0,
    )
    assert repaired["ready"] is True
    assert [
        item["memory_id"]
        for item in governance_store.search_operational_memory("needle", top_k=10)
    ] == ["memory_3"]


def _integrity_test_records(count=4):
    return [
        {
            "memory_id": f"memory_{index}",
            "memory_type": "fact",
            "memory_key": f"integrity_key_{index}",
            "text": "rare proof needle" if index == count - 1 else "unrelated",
            "source_page": "Concept_Integrity.md",
            "validity_state": "active",
            "memory_score": 0.8,
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
        for index in range(count)
    ]


def _tamper_memory_fts_with_equal_counts(memory_id, *, text="stale tokens"):
    conn = db_store.get_connection()
    doc_id = int(
        conn.execute(
            "SELECT doc_id FROM operational_memory_search_docs WHERE memory_id = ?",
            (memory_id,),
        ).fetchone()[0]
    )
    external = sqlite3.connect(str(db_store.get_db_path()))
    try:
        external.execute(
            "DELETE FROM operational_memory_search_fts WHERE rowid = ?",
            (doc_id,),
        )
        external.execute(
            "INSERT INTO operational_memory_search_fts "
            "(rowid, key_text, memory_text, page_text, type_text) "
            "VALUES (?, 'stale', ?, 'stale', 'fact')",
            (doc_id, text),
        )
        external.execute(
            "DELETE FROM operational_memory_search_short_fts WHERE rowid = ?",
            (doc_id,),
        )
        external.execute(
            "INSERT INTO operational_memory_search_short_fts (rowid, short_text) "
            "VALUES (?, ?)",
            (doc_id, text),
        )
        external.commit()
    finally:
        external.close()


def test_equal_count_fts_tamper_fails_closed_and_replays_within_batch_budget(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_ATTESTATION_SECONDS", "0")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_DEGRADED_ROW_LIMIT", "3")
    conn = _seed_isolated_search_memories(_integrity_test_records())
    assert governance_store.maintain_operational_memory_search_index(100)["ready"]
    _tamper_memory_fts_with_equal_counts("memory_3")

    status = governance_store.operational_memory_search_index_status()
    assert status["ready"] is False
    assert "operational_memory_search_integrity" in status["warnings"]
    assert status["canonical_documents"] == status["indexed_documents"] == 4
    with pytest.raises(governance_store.OperationalMemoryNotReady) as error:
        governance_store.search_operational_memory("needle", top_k=10)
    assert error.value.reason == "search_index_integrity_mismatch"

    first = governance_store.maintain_operational_memory_search_index(2)
    assert first["ready"] is False
    cursor = conn.execute(
        "SELECT backfill_cursor FROM operational_memory_search_state"
    ).fetchone()[0]
    assert cursor == "memory_1"
    second = governance_store.maintain_operational_memory_search_index(2)
    assert second["ready"] is True
    assert [
        item["memory_id"]
        for item in governance_store.search_operational_memory("needle", top_k=10)
    ] == ["memory_3"]


def test_canonical_drift_with_lost_pending_fails_closed_and_replays(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_DEGRADED_ROW_LIMIT", "3")
    records = _integrity_test_records()
    conn = _seed_isolated_search_memories(records)
    assert governance_store.maintain_operational_memory_search_index(100)["ready"]
    changed = dict(records[-1])
    changed["text"] = "canonical drift replacement"
    changed["updated_at"] = "2026-01-02T00:00:00+00:00"
    with db_store.transaction():
        conn.execute(
            "UPDATE operational_memory SET data_json = ?, updated_at = ? "
            "WHERE memory_id = ?",
            (
                governance_store.json.dumps(changed, ensure_ascii=False),
                changed["updated_at"],
                changed["memory_id"],
            ),
        )
        conn.execute(
            "DELETE FROM operational_memory_search_pending WHERE memory_id = ?",
            (changed["memory_id"],),
        )

    with pytest.raises(governance_store.OperationalMemoryNotReady) as error:
        governance_store.search_operational_memory("replacement", top_k=10)
    assert error.value.reason == "search_index_integrity_mismatch"
    repaired = governance_store.maintain_operational_memory_search_index_budget(
        batch_size=2,
        max_batches=2,
        wall_seconds=2.0,
    )
    assert repaired["ready"] is True
    assert [
        item["memory_id"]
        for item in governance_store.search_operational_memory("replacement", top_k=10)
    ] == ["memory_3"]


def test_operational_memory_query_race_discards_fts_results(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_DEGRADED_ROW_LIMIT", "3")
    _seed_isolated_search_memories(_integrity_test_records())
    assert governance_store.maintain_operational_memory_search_index(100)["ready"]
    real_decode = governance_store._decode_operational_memory_json
    tampered = False

    def tamper_during_query(payload):
        nonlocal tampered
        if not tampered:
            tampered = True
            _tamper_memory_fts_with_equal_counts("memory_3", text="raced tokens")
        return real_decode(payload)

    monkeypatch.setattr(
        governance_store,
        "_decode_operational_memory_json",
        tamper_during_query,
    )
    with pytest.raises(governance_store.OperationalMemoryNotReady) as error:
        governance_store.search_operational_memory("needle", top_k=10)
    assert tampered is True
    assert error.value.reason == "search_index_integrity_race"


def test_certification_gap_external_tamper_cannot_become_new_proof(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_DEGRADED_ROW_LIMIT", "3")
    records = _integrity_test_records()
    conn = _seed_isolated_search_memories(records)
    assert governance_store.maintain_operational_memory_search_index(100)["ready"]
    changed = dict(records[-1])
    changed["text"] = "post commit canonical value"
    changed["updated_at"] = "2026-01-02T00:00:00+00:00"
    with db_store.transaction():
        conn.execute(
            "UPDATE operational_memory SET data_json = ?, updated_at = ? "
            "WHERE memory_id = ?",
            (
                governance_store.json.dumps(changed, ensure_ascii=False),
                changed["updated_at"],
                changed["memory_id"],
            ),
        )
    injected = []

    def tamper_in_unlocked_gap():
        if not injected:
            injected.append(1)
            _tamper_memory_fts_with_equal_counts(
                changed["memory_id"],
                text="gap tamper must never certify",
            )

    monkeypatch.setattr(
        governance_store,
        "_operational_memory_search_certification_gap_hook",
        tamper_in_unlocked_gap,
    )
    result = governance_store.maintain_operational_memory_search_index(1)
    assert injected == [1]
    assert result["ready"] is False
    state = conn.execute(
        "SELECT backfill_cursor, backfill_target, proof_status, proof_generation "
        "FROM operational_memory_search_state"
    ).fetchone()
    assert tuple(state) == ("", "memory_3", "rebuild_required", None)
    with pytest.raises(governance_store.OperationalMemoryNotReady) as error:
        governance_store.search_operational_memory("canonical", top_k=10)
    assert error.value.reason == "search_index_backfilling"

    monkeypatch.setattr(
        governance_store,
        "_operational_memory_search_certification_gap_hook",
        lambda: None,
    )
    repaired = governance_store.maintain_operational_memory_search_index_budget(
        batch_size=2,
        max_batches=2,
        wall_seconds=2.0,
    )
    assert repaired["ready"] is True
    assert [
        item["memory_id"]
        for item in governance_store.search_operational_memory("canonical", top_k=10)
    ] == ["memory_3"]


def test_complete_unready_rebuild_replays_before_certifying_existing_fts(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_DEGRADED_ROW_LIMIT", "3")
    conn = _seed_isolated_search_memories(_integrity_test_records())
    assert governance_store.maintain_operational_memory_search_index(100)["ready"]

    with db_store.transaction():
        db_store.mark_operational_memory_search_rebuild_required(conn)
    real_certify = governance_store.certify_operational_memory_search_integrity

    def fail_certification(_conn):
        raise RuntimeError("injected certification failure")

    monkeypatch.setattr(
        governance_store,
        "certify_operational_memory_search_integrity",
        fail_certification,
    )
    failed = governance_store.maintain_operational_memory_search_index(100)
    assert failed["ready"] is False
    state = conn.execute(
        "SELECT backfill_cursor, backfill_target, proof_status, proof_generation "
        "FROM operational_memory_search_state"
    ).fetchone()
    assert tuple(state) == (
        "memory_3",
        "memory_3",
        "rebuild_required",
        None,
    )

    monkeypatch.setattr(
        governance_store,
        "certify_operational_memory_search_integrity",
        real_certify,
    )
    _tamper_memory_fts_with_equal_counts(
        "memory_3",
        text="complete unready tamper must not certify",
    )

    first = governance_store.maintain_operational_memory_search_index(2)
    assert first["ready"] is False
    state = conn.execute(
        "SELECT backfill_cursor, backfill_target, proof_status, proof_generation "
        "FROM operational_memory_search_state"
    ).fetchone()
    assert tuple(state) == (
        "memory_1",
        "memory_3",
        "rebuild_required",
        None,
    )
    second = governance_store.maintain_operational_memory_search_index(2)
    assert second["ready"] is True
    assert [
        item["memory_id"]
        for item in governance_store.search_operational_memory("needle", top_k=10)
    ] == ["memory_3"]


def test_operational_memory_integrity_limit_fails_closed_without_rebuild(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_DEGRADED_ROW_LIMIT", "3")
    conn = _seed_isolated_search_memories(_integrity_test_records())
    assert governance_store.maintain_operational_memory_search_index(100)["ready"]
    state_before = tuple(
        conn.execute(
            "SELECT backfill_cursor, backfill_target, proof_status "
            "FROM operational_memory_search_state"
        ).fetchone()
    )
    db_store.close_all_connections()
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_INTEGRITY_MAX_BYTES", "1")

    with pytest.raises(governance_store.OperationalMemoryNotReady) as error:
        governance_store.search_operational_memory("needle", top_k=10)
    assert error.value.reason == "search_index_integrity_limit"
    result = governance_store.maintain_operational_memory_search_index(2)
    assert result["ready"] is False
    conn = db_store.get_connection()
    state_after = tuple(
        conn.execute(
            "SELECT backfill_cursor, backfill_target, proof_status "
            "FROM operational_memory_search_state"
        ).fetchone()
    )
    assert state_after == state_before


def test_operational_memory_integrity_scan_is_cached_for_hot_queries(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    _seed_isolated_search_memories(_integrity_test_records())
    assert governance_store.maintain_operational_memory_search_index(100)["ready"]
    db_store.close_all_connections()
    real_inspect = db_store.inspect_operational_memory_search_integrity
    inspections = []

    def counted_inspect(*args, **kwargs):
        inspections.append(1)
        return real_inspect(*args, **kwargs)

    monkeypatch.setattr(
        db_store,
        "inspect_operational_memory_search_integrity",
        counted_inspect,
    )
    first = governance_store.search_operational_memory("needle", top_k=10)
    second = governance_store.search_operational_memory("needle", top_k=10)
    assert second == first
    assert inspections == [1]


def test_unrelated_external_commit_does_not_rescan_operational_memory_integrity(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    conn = _seed_isolated_search_memories(_integrity_test_records())
    with db_store.transaction():
        conn.execute(
            "CREATE TABLE operational_memory_unrelated_probe "
            "(probe_id INTEGER PRIMARY KEY, payload TEXT)"
        )
    assert governance_store.maintain_operational_memory_search_index(100)["ready"]
    db_store.close_all_connections()
    real_inspect = db_store.inspect_operational_memory_search_integrity
    inspections = []

    def counted_inspect(*args, **kwargs):
        inspections.append(1)
        return real_inspect(*args, **kwargs)

    monkeypatch.setattr(
        db_store,
        "inspect_operational_memory_search_integrity",
        counted_inspect,
    )
    first = governance_store.search_operational_memory("needle", top_k=10)
    external = sqlite3.connect(str(db_store.get_db_path()))
    try:
        external.execute(
            "INSERT INTO operational_memory_unrelated_probe "
            "(probe_id, payload) VALUES (1, 'unrelated')"
        )
        external.commit()
    finally:
        external.close()
    second = governance_store.search_operational_memory("needle", top_k=10)

    assert second == first
    assert inspections == [1]


def test_relevant_external_commit_invalidates_integrity_cache_immediately(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    _seed_isolated_search_memories(_integrity_test_records())
    assert governance_store.maintain_operational_memory_search_index(100)["ready"]
    db_store.close_all_connections()
    real_inspect = db_store.inspect_operational_memory_search_integrity
    inspections = []

    def counted_inspect(*args, **kwargs):
        inspections.append(1)
        return real_inspect(*args, **kwargs)

    monkeypatch.setattr(
        db_store,
        "inspect_operational_memory_search_integrity",
        counted_inspect,
    )
    assert governance_store.operational_memory_search_index_status()["ready"]
    external = sqlite3.connect(str(db_store.get_db_path()))
    try:
        external.execute(
            "UPDATE operational_memory_search_docs "
            "SET source_updated_at = source_updated_at WHERE memory_id = ?",
            ("memory_0",),
        )
        external.commit()
    finally:
        external.close()
    assert governance_store.operational_memory_search_index_status()["ready"]

    assert inspections == [1, 1]


def test_direct_fts_tamper_is_detected_by_periodic_attestation(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    monkeypatch.setenv(
        "VECTOR_LAKE_OPERATIONAL_MEMORY_ATTESTATION_SECONDS",
        "60",
    )
    clock = [100.0]
    monkeypatch.setattr(db_store.time, "monotonic", lambda: clock[0])
    _seed_isolated_search_memories(_integrity_test_records())
    assert governance_store.maintain_operational_memory_search_index(100)["ready"]
    db_store.close_all_connections()
    real_inspect = db_store.inspect_operational_memory_search_integrity
    inspections = []

    def counted_inspect(*args, **kwargs):
        inspections.append(1)
        return real_inspect(*args, **kwargs)

    monkeypatch.setattr(
        db_store,
        "inspect_operational_memory_search_integrity",
        counted_inspect,
    )
    assert governance_store.operational_memory_search_index_status()["ready"]
    _tamper_memory_fts_with_equal_counts("memory_3")
    clock[0] = 159.0
    assert governance_store.operational_memory_search_index_status()["ready"]
    assert inspections == [1]

    clock[0] = 160.0
    status = governance_store.operational_memory_search_index_status()
    assert status["ready"] is False
    assert "operational_memory_search_integrity" in status["warnings"]
    assert inspections == [1, 1]


def test_invalid_persisted_proof_fails_closed_and_bounded_maintenance_recovers(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_DEGRADED_ROW_LIMIT", "3")
    conn = _seed_isolated_search_memories(_integrity_test_records())
    assert governance_store.maintain_operational_memory_search_index(100)["ready"]
    with db_store.transaction():
        conn.execute(
            "UPDATE operational_memory_search_state SET proof_generation = NULL"
        )

    with pytest.raises(governance_store.OperationalMemoryNotReady) as error:
        governance_store.search_operational_memory("needle", top_k=10)
    assert error.value.reason == "search_index_integrity_state"
    repaired = governance_store.maintain_operational_memory_search_index_budget(
        batch_size=2,
        max_batches=2,
        wall_seconds=2.0,
    )
    assert repaired["ready"] is True
    assert governance_store.operational_memory_search_index_status()[
        "proof_status"
    ] == "ready"


def test_ready_index_orphan_is_removed_within_maintenance_budget(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    conn = _seed_isolated_search_memories([
        {
            "memory_id": "memory_valid",
            "memory_type": "fact",
            "memory_key": "valid",
            "text": "valid memory",
            "source_page": "Concept_Repair.md",
            "validity_state": "active",
            "memory_score": 0.8,
        }
    ])
    assert governance_store.maintain_operational_memory_search_index(10)["ready"]
    with db_store.transaction():
        conn.execute(
            "INSERT INTO operational_memory_search_docs "
            "(memory_id, source_updated_at) VALUES (?, ?)",
            ("memory_orphan", "2026-01-01T00:00:00+00:00"),
        )

    assert governance_store.operational_memory_search_index_status()["ready"] is False
    repaired = governance_store.maintain_operational_memory_search_index(1)
    assert repaired["ready"] is True
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_docs "
        "WHERE memory_id = 'memory_orphan'"
    ).fetchone()[0] == 0

    with db_store.transaction():
        conn.execute(
            "INSERT INTO operational_memory_search_fts "
            "(rowid, key_text, memory_text, page_text, type_text) "
            "VALUES (999, 'orphan', 'orphan', 'orphan', 'fact')"
        )
        conn.execute(
            "INSERT INTO operational_memory_search_short_fts (rowid, short_text) "
            "VALUES (999, 'orphan')"
        )
    assert governance_store.operational_memory_search_index_status()["ready"] is False
    repaired = governance_store.maintain_operational_memory_search_index(2)
    assert repaired["ready"] is True
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_fts WHERE rowid = 999"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_short_fts WHERE rowid = 999"
    ).fetchone()[0] == 0


def test_large_disabled_index_requires_explicit_legacy_opt_in(
    isolated_memory, monkeypatch
):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "0")
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_DEGRADED_ROW_LIMIT", "2")
    records = [
        {
            "memory_id": f"memory_{index}",
            "memory_type": "fact",
            "memory_key": f"key_{index}",
            "text": "legacy opt in",
            "source_page": "Concept_Legacy.md",
            "validity_state": "active",
            "memory_score": 0.8,
        }
        for index in range(3)
    ]
    _seed_isolated_search_memories(records)

    with pytest.raises(governance_store.OperationalMemoryNotReady) as error:
        governance_store.search_operational_memory("legacy", top_k=10)
    assert error.value.reason == "search_index_disabled"

    monkeypatch.setenv(
        "VECTOR_LAKE_OPERATIONAL_MEMORY_ALLOW_UNBOUNDED_FALLBACK",
        "1",
    )
    assert len(governance_store.search_operational_memory("legacy", top_k=10)) == 3


def test_search_index_status_reports_stalled_progress(isolated_memory, monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_OPERATIONAL_MEMORY_FTS", "1")
    conn = _seed_isolated_search_memories([
        {
            "memory_id": "memory_stalled",
            "memory_type": "fact",
            "memory_key": "stalled",
            "text": "stalled progress",
            "source_page": "Concept_Stalled.md",
            "validity_state": "active",
            "memory_score": 0.8,
        }
    ])
    _reset_search_index_as_legacy(conn)
    with db_store.transaction():
        conn.execute(
            "UPDATE operational_memory_search_state SET updated_at = ?",
            ("2020-01-01T00:00:00+00:00",),
        )

    status = governance_store.operational_memory_search_index_status()

    assert status["progress_stalled"] is True
    assert any(
        warning.startswith("operational_memory_search_progress_stalled:")
        for warning in status["warnings"]
    )


def test_rc_mcp_configs_enable_operational_memory_fts():
    root = Path(__file__).resolve().parents[1]
    for filename in (".mcp.json", "mcp_config.json"):
        payload = governance_store.json.loads(
            (root / filename).read_text(encoding="utf-8")
        )
        env = payload["mcpServers"]["vector-lake-mcp"]["env"]
        assert env["VECTOR_LAKE_OPERATIONAL_MEMORY_FTS"] == "1"




if __name__ == "__main__":
    unittest.main()
