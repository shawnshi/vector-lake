"""Regression tests for CJK-safe merge candidate detection.

The previous ``[^a-z0-9]`` normalizer erased every Chinese name to "", which put
all CJK entities in one bucket: 10 unrelated entities produced 45/45 pairs of
false "normalized-name-match" candidates.
"""
from vector_lake import db_store, governance_store
from vector_lake.governance_metrics import _normalized_name, find_merge_candidates


CJK_NAMES = [
    "北京协和医院", "上海瑞金医院", "广州中山医院", "华西医院", "浙大一院",
    "医惠科技", "卫宁健康", "东华医为", "创业慧康", "思创医惠",
]


def _institution(index, name, entity_id=None):
    entity_id = entity_id or f"entity_{index:032x}"
    return entity_id, {
        "entity_id": entity_id,
        "page_key": f"Institution_{index}",
        "canonical_name": name,
        "type": "institution",
        "domain": "Healthcare_IT",
        "topic_cluster": "Hospital",
        "status": "Active",
        "aliases": [],
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


def _seed(memory_dir, names):
    db_store.init_db()
    items = dict(_institution(index, name) for index, name in enumerate(names))
    governance_store.save_entities({"items": items, "updated_at": "2026-01-01T00:00:00+00:00"})
    return items


def test_cjk_names_normalize_to_distinct_non_empty_keys():
    keys = {_normalized_name(name) for name in CJK_NAMES}
    assert len(keys) == len(CJK_NAMES)
    assert "" not in keys


def test_normalization_folds_width_and_spacing_variants():
    assert _normalized_name("Ａｃｍｅ　ＨＩＳ") == _normalized_name("Acme HIS")
    assert _normalized_name("Acme-HIS Inc") == _normalized_name("Acme HIS Inc".replace(" ", ""))


def test_normalization_never_collapses_on_empty_key():
    assert _normalized_name("———") != ""
    assert _normalized_name("北京协和医院") != _normalized_name("上海瑞金医院")


def test_unrelated_cjk_entities_produce_no_merge_candidates(isolated_memory):
    _seed(isolated_memory, CJK_NAMES)

    assert find_merge_candidates(limit=100) == []


def test_genuine_duplicate_is_still_detected(isolated_memory):
    items = _seed(isolated_memory, CJK_NAMES[:3])
    entity_id, duplicate = _institution(99, "北京协和医院", entity_id="entity_duplicate")
    duplicate["page_key"] = "Institution_duplicate"
    items[entity_id] = duplicate
    governance_store.save_entities({"items": items, "updated_at": "2026-01-01T00:00:00+00:00"})

    candidates = find_merge_candidates(limit=100)

    assert len(candidates) == 1
    # ``left_name``/``right_name`` now carry the on-disk page key; the CJK title
    # stays available on the display fields.
    assert "北京协和医院" in candidates[0]["left_canonical_name"] + candidates[0]["right_canonical_name"]
