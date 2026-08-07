import os
import random
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pytest

from vector_lake import db_store, governance_store, wiki_utils
from vector_lake.tool_search import build_memory_packet, search_vector_lake


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

def test_search_index_schema_upgrade_resets_only_derived_state(
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
            "UPDATE operational_memory_search_state SET schema_version = 2"
        )
        assert db_store._init_operational_memory_search_schema(conn) is True

    state = conn.execute(
        "SELECT backfill_cursor, backfill_target, schema_version "
        "FROM operational_memory_search_state"
    ).fetchone()
    assert tuple(state) == ("", "memory_upgrade", 4)
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_docs"
    ).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM operational_memory").fetchone()[0] == 1
    assert [
        item["memory_id"]
        for item in governance_store.search_operational_memory("upgrade", top_k=10)
    ] == ["memory_upgrade"]


@pytest.mark.parametrize("legacy_schema_version", [3, 4])
def test_search_index_v4_rebuilds_legacy_integer_cursor_affinity(
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
    assert tuple(state) == ("", "010", 4)
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
    assert tuple(state) == ("", "", 4)
    assert conn.execute(
        "SELECT COUNT(*) FROM operational_memory_search_docs"
    ).fetchone()[0] == 0

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




if __name__ == "__main__":
    unittest.main()
