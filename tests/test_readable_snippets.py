import json
import sqlite3

from vector_lake import tool_search


def _plaintext_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE entities (entity_id TEXT PRIMARY KEY, canonical_name TEXT, data_json TEXT);"
        "CREATE TABLE entity_identities (entity_id TEXT PRIMARY KEY, page_key TEXT, data_json TEXT);"
    )
    return conn


def test_safe_prefix_preserves_or_removes_whole_singular_and_plural_anchors():
    for header in ("Source", "Sources", "sOuRcEs"):
        for closing in (")", " )"):
            anchor = f"({header}: [[Source_A(v2)]]{closing}"
            text = "Evidence. " + anchor
            start = text.index("(")
            for limit in range(start + 1, len(text)):
                assert tool_search._safe_evidence_prefix(text, limit) == "Evidence."
            assert tool_search._safe_evidence_prefix(text, len(text)) == text


def test_complete_source_anchor_with_closing_whitespace_is_not_false_incomplete():
    for header in ("Source", "Sources", "sOuRcEs"):
        for whitespace in (" ", "\t", "\n"):
            anchor = f"({header}: [[Source_A]]{whitespace})"
            text = "Focus evidence. " + anchor + " Tail material." * 80
            for limit in (80, 128, 256):
                snippet, _, clipped = tool_search._query_centered_snippet(text, "Focus", limit=limit)
                assert anchor in snippet
                assert "incomplete" not in snippet
                assert len(snippet) <= limit
                assert clipped is True
            snippet, _, _ = tool_search._query_centered_snippet(text, "Focus", limit=24)
            assert anchor not in snippet
            assert "incomplete" in snippet
            assert len(snippet) <= 24


def test_plural_anchor_stays_atomic_when_missing_refs_force_budget_cut():
    text = "关键查询证据。 (Sources: [[Source_A]])[^missing]"
    anchor = "(Sources: [[Source_A]])"
    for limit in (24, 32, 48, 96):
        snippet, _, clipped = tool_search._bounded_namespaced_snippet(
            text, "关键查询", "Source_甲", limit,
        )
        assert len(snippet) <= limit
        assert "关键查询" in snippet
        if "(Sources:" in snippet:
            assert anchor in snippet
        assert snippet.count("[^") == len(__import__("re").findall(r"\[\^[^\]\r\n]+\]", snippet))
        assert "incomplete" in snippet or "[!]" in snippet


def test_plaintext_loader_joins_historical_identity_to_live_entity():
    conn = _plaintext_db()
    live = {
        "status": "active",
        "title": "原始标题",
        "raw_text": "开头。这里有完整的否定：不得删除原始证据。结尾。",
    }
    conn.execute(
        "INSERT INTO entities VALUES (?, ?, ?)",
        ("e1", "原始标题", json.dumps(live, ensure_ascii=False)),
    )
    conn.execute(
        "INSERT INTO entity_identities VALUES (?, ?, ?)",
        ("e1", "Concept_Current", json.dumps({"raw_text": "分 词 假 正 文"})),
    )

    rows = tool_search._load_current_plaintext_rows(
        conn, ["Concept_Current"], "不得删除", snippet_limit=200,
    )

    assert "不得删除原始证据" in rows["Concept_Current"]["snippet"]
    assert "分 词 假 正 文" not in rows["Concept_Current"]["snippet"]


def test_deleted_identity_without_live_entity_never_renders_history():
    conn = _plaintext_db()
    conn.execute(
        "INSERT INTO entity_identities VALUES (?, ?, ?)",
        ("deleted", "Concept_Deleted", '{"raw_text":"historical secret"}'),
    )

    rows = tool_search._load_current_plaintext_rows(
        conn, ["Concept_Deleted"], "secret", snippet_limit=200,
    )

    assert rows["Concept_Deleted"]["status"] == "missing"
    assert rows["Concept_Deleted"]["snippet"] == ""


def test_query_centered_snippet_finds_unicode_tail_after_boilerplate():
    text = ("<!-- generated navigation -->\n\n" + "前置材料。" * 9000
            + "Unicode café 结论：不能忽略风险。[^证据]\n\n[^证据]: 原始脚注。"
            + "尾部。" * 8000)
    snippet, status, clipped = tool_search._query_centered_snippet(
        text, "不能忽略风险", limit=300,
    )

    assert "不能忽略风险" in snippet
    assert status in {"available", "source_window_clipped"}
    assert "原始脚注" in snippet or "footnote incomplete" in snippet
    assert clipped is True


def test_query_centered_snippet_reports_hit_beyond_scan_cap():
    text = "前" * (tool_search._PLAINTEXT_SCAN_CHAR_LIMIT + 10) + "needle"

    snippet, status, clipped = tool_search._query_centered_snippet(
        text, "needle", limit=200,
    )

    assert snippet == ""
    assert status == "hit_not_located_scan_clipped"
    assert clipped is True


def test_sentence_boundary_keeps_attached_footnote():
    text = "前言。关键结论不得删除证据。[^ref]\n\n[^ref]: 审计原文。"

    snippet, _status, _clipped = tool_search._query_centered_snippet(
        text, "不得删除", limit=80,
    )

    assert "。[^ref]" in snippet
    assert "审计原文" in snippet or "footnote incomplete" in snippet
    assert len(snippet) <= 80


def test_sentence_boundary_keeps_consecutive_and_newline_footnotes():
    text = (
        "前言。关键结论不得删除证据。 \n[^a][^b]\n\n"
        "[^a]: 第一份审计原文。\n[^b]: 第二份审计原文。"
    )

    snippet, _status, _clipped = tool_search._query_centered_snippet(
        text, "不得删除", limit=140,
    )

    assert "[^a][^b]" in snippet
    assert "[^a]:" in snippet and "[^b]:" in snippet
    assert len(snippet) <= 140


def test_sentence_boundary_keeps_parenthesized_source_anchor_atomically():
    anchor = "(Source: [[Source_Project_(Phase_2)]])"
    text = f"前言。关键结论不得删除证据。 {anchor} 后续说明。"

    snippet, _status, _clipped = tool_search._query_centered_snippet(
        text, "不得删除", limit=90,
    )

    assert anchor in snippet
    assert snippet.count("(") == snippet.count(")")


def test_bounded_citation_closure_accepts_mixed_sequences_and_separators():
    source_a = "(Source: [[Source_A(v2)]])"
    source_b = "(Sources: [[Source_B]])"
    sequences = (
        f"[^a][^b]{source_a}",
        f"{source_a} {source_b}",
        f"{source_a}[^a]{source_b}",
    )
    separators = ("", " ", "\n", ", ", ";\n")

    for sequence in sequences:
        for separator in separators:
            value = separator.join(
                part for part in (
                    "[^a]" if "[^a]" in sequence else "",
                    source_a if source_a in sequence else "",
                    "[^b]" if "[^b]" in sequence else "",
                    source_b if source_b in sequence else "",
                ) if part
            )
            end, incomplete = tool_search._bounded_citation_closure(value, 0)
            assert end == len(value)
            assert incomplete is False


def test_citation_closure_budget_keeps_every_atom_or_marks_incomplete():
    import re

    closure = "[^a][^b](Source: [[Source_A(v2)]])"
    text = f"前言。关键结论不得删除证据。 {closure} 后续说明。"
    for limit in (42, 64, 100):
        snippet, _status, _clipped = tool_search._query_centered_snippet(
            text, "不得删除", limit=limit,
        )
        assert len(snippet) <= limit
        assert snippet.count("[^") == len(re.findall(r"\[\^[^\]\r\n]+\]", snippet))
        assert snippet.count("(Source:") == snippet.count("]])")
        if closure not in snippet:
            assert "incomplete" in snippet or "[!]" in snippet


def test_citation_closure_does_not_consume_prose_or_footnote_definition():
    for tail in (" plain prose", "\n[^a]: definition text"):
        text = "[^ok]" + tail
        end, incomplete = tool_search._bounded_citation_closure(text, 0)
        assert text[:end] == "[^ok]"
        assert incomplete is False


def test_citation_closure_marks_malformed_source_and_scan_limits():
    malformed = "(Source: [[Source_A(v2)])"
    end, incomplete = tool_search._bounded_citation_closure(malformed, 0)
    assert end == 0
    assert incomplete is True
    snippet, _status, _clipped = tool_search._query_centered_snippet(
        f"前言。关键结论不得删除证据。 {malformed}", "不得删除", limit=96,
    )
    assert "incomplete" in snippet or "[!]" in snippet
    assert "(Source:" not in snippet

    too_many = "".join(f"[^r{index}]" for index in range(33))
    end, incomplete = tool_search._bounded_citation_closure(too_many, 0)
    assert end < len(too_many)
    assert incomplete is True

    long_gap = "[^a]" + " " * 33 + "[^b]"
    end, incomplete = tool_search._bounded_citation_closure(long_gap, 0)
    assert long_gap[:end] == "[^a]"
    assert incomplete is True


def test_newline_boundary_mixed_closure_is_complete_or_disclosed():
    closure = "(Source: [[Source_A(v2)]])\n[^a]; (Sources: [[Source_B]])"
    text = f"关键结论不得删除证据。\n{closure}\n后续说明。"
    for limit in (48, 160):
        snippet, _status, _clipped = tool_search._query_centered_snippet(
            text, "不得删除", limit=limit,
        )
        assert len(snippet) <= limit
        assert closure in snippet or "incomplete" in snippet or "[!]" in snippet
        assert "(Source:" not in snippet or "]])" in snippet


def test_long_footnote_label_never_breaks_tiny_budget():
    label = "超长标签" * 2_000
    text = f"关键否定：不得忽略。[^{label}]"

    snippet, status, clipped = tool_search._query_centered_snippet(
        text, "不得忽略", limit=24,
    )

    assert len(snippet) <= 24
    assert status == "source_window_clipped"
    assert clipped is True


def test_actual_cursor_metadata_yields_real_content_and_tail_match():
    text = (
        "# Cursor\n\n"
        "*[System Directive: This section represents generated navigation]*\n\n"
        "This document was automatically generated by Cursor for navigation.\n\n"
        "generic query placeholder\n\n"
        "真实段落解释 generic query，并明确不得删除原始证据。"
    )

    snippet, _status, _clipped = tool_search._query_centered_snippet(
        text, "generic query 不得删除", limit=180,
    )

    assert "不得删除原始证据" in snippet
    assert snippet.strip() != "# Cursor"
    assert "System Directive" not in snippet


def test_quoted_directive_discussion_is_preserved():
    text = (
        "> [System Directive: This section represents a quoted incident]\n"
        "> 分析认为不得忽略该指令。"
    )

    snippet, _status, _clipped = tool_search._query_centered_snippet(
        text, "不得忽略", limit=120,
    )

    assert "不得忽略该指令" in snippet


def test_query_literal_patterns_are_bounded_and_unicode_offsets_are_source_offsets():
    query = " ".join(f"term{index}" for index in range(100))
    patterns = tool_search._query_literal_patterns(query)

    assert len(patterns) <= tool_search._QUERY_PATTERN_LIMIT
    source = "前缀 café 结论"
    match = tool_search._query_literal_patterns("CAFÉ")[0].search(source)
    assert match is not None
    assert source[match.start():match.end()] == "café"


def test_plaintext_loader_namespaces_same_footnote_id_per_page():
    conn = _plaintext_db()
    for entity_id, page_key, definition in (
        ("e1", "Page_One", "第一页定义"),
        ("e2", "Page_Two", "第二页定义"),
    ):
        record = {
            "status": "active",
            "raw_text": f"共同结论。[^same]\n\n[^same]: {definition}",
        }
        conn.execute(
            "INSERT INTO entities VALUES (?, ?, ?)",
            (entity_id, page_key, json.dumps(record, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO entity_identities VALUES (?, ?, '{}')",
            (entity_id, page_key),
        )

    rows = tool_search._load_current_plaintext_rows(
        conn, ["Page_One", "Page_Two"], "共同结论", snippet_limit=120,
    )

    namespace_one = tool_search._footnote_namespace("Page_One")
    namespace_two = tool_search._footnote_namespace("Page_Two")
    assert f"[^{namespace_one}--same]" in rows["Page_One"]["snippet"]
    assert f"[^{namespace_two}--same]" in rows["Page_Two"]["snippet"]


def test_plaintext_loader_namespaces_colliding_unicode_and_long_keys_distinctly():
    conn = _plaintext_db()
    prefix = "Source_" + "A" * 80
    page_keys = ["Source_甲", "Source_乙", prefix + "甲", prefix + "乙"]
    for index, page_key in enumerate(page_keys):
        record = {
            "status": "active",
            "raw_text": f"共同结论。[^same]\n\n[^same]: 定义{index}",
        }
        conn.execute(
            "INSERT INTO entities VALUES (?, ?, ?)",
            (f"e{index}", page_key, json.dumps(record, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO entity_identities VALUES (?, ?, '{}')",
            (f"e{index}", page_key),
        )

    rows = tool_search._load_current_plaintext_rows(
        conn, page_keys, "共同结论", snippet_limit=120,
    )

    namespaces = {tool_search._footnote_namespace(key) for key in page_keys}
    assert len(namespaces) == len(page_keys)
    for index, page_key in enumerate(page_keys):
        namespace = tool_search._footnote_namespace(page_key)
        snippet = rows[page_key]["snippet"]
        assert f"[^{namespace}--same]" in snippet
        assert f"[^{namespace}--same]: 定义{index}" in snippet
        assert len(snippet) <= 120


def test_plaintext_loader_final_budget_survives_many_namespaced_references():
    import re

    conn = _plaintext_db()
    refs = "".join(f"[^r{index}]" for index in range(18))
    definitions = "\n".join(f"[^r{index}]: 定义{index}" for index in range(18))
    record = {
        "status": "active",
        "raw_text": f"关键查询证据。 {refs}\n\n{definitions}",
    }
    page_key = "Source_" + "very-long-key-" * 12
    conn.execute(
        "INSERT INTO entities VALUES (?, ?, ?)",
        ("many", page_key, json.dumps(record, ensure_ascii=False)),
    )
    conn.execute(
        "INSERT INTO entity_identities VALUES (?, ?, '{}')", ("many", page_key),
    )

    for limit in (24, 96, 257):
        result = tool_search._load_current_plaintext_rows(
            conn, [page_key], "关键 证据", snippet_limit=limit,
        )[page_key]
        snippet = result["snippet"]
        assert len(snippet) <= limit
        assert "关键查询证据" in snippet
        assert result["truncated"] is True
        complete_refs = re.findall(r"\[\^[^\]\r\n]+\]", snippet)
        assert snippet.count("[^") == len(complete_refs)
        if not complete_refs:
            assert "incomplete" in snippet


def test_plaintext_loader_reports_corrupt_and_oversized_records():
    conn = _plaintext_db()
    conn.executemany(
        "INSERT INTO entities VALUES (?, ?, ?)",
        [
            ("bad", "Bad", "{"),
            ("large", "Large", "x" * (tool_search._PLAINTEXT_RECORD_BYTE_LIMIT + 1)),
        ],
    )
    conn.executemany(
        "INSERT INTO entity_identities VALUES (?, ?, '{}')",
        [("bad", "Concept_Bad"), ("large", "Concept_Large")],
    )

    rows = tool_search._load_current_plaintext_rows(
        conn, ["Concept_Bad", "Concept_Large"], "x", snippet_limit=100,
    )

    assert rows["Concept_Bad"]["status"] == "corrupt"
    assert rows["Concept_Large"]["status"] == "oversized"


def test_plaintext_loader_reports_nonobject_json_as_corrupt():
    conn = _plaintext_db()
    conn.executemany(
        "INSERT INTO entities VALUES (?, ?, ?)",
        [("list", "List", "[]"), ("null", "Null", "null")],
    )
    conn.executemany(
        "INSERT INTO entity_identities VALUES (?, ?, '{}')",
        [("list", "Concept_List"), ("null", "Concept_Null")],
    )

    rows = tool_search._load_current_plaintext_rows(
        conn, ["Concept_List", "Concept_Null"], "x", snippet_limit=100,
    )

    assert rows["Concept_List"]["status"] == "corrupt"
    assert rows["Concept_Null"]["status"] == "corrupt"


def test_structural_page_is_omitted_normally_but_preserved_when_exact():
    key = "Concept_Orphan-Index"
    assert tool_search._is_structural_noise(key, "ordinary knowledge", set()) is True
    assert tool_search._is_structural_noise(key, key, {key}) is False


def test_plaintext_batch_marks_exact_title_and_alias_without_identity_scan():
    conn = _plaintext_db()
    record = {
        "status": "active",
        "title": "Cursor 运行报告",
        "aliases": ["Cursor Report"],
        "raw_text": "真实系统报告正文。",
    }
    conn.execute(
        "INSERT INTO entities VALUES (?, ?, ?)",
        ("e-report", "Cursor 运行报告", json.dumps(record, ensure_ascii=False)),
    )
    conn.execute(
        "INSERT INTO entity_identities VALUES (?, ?, '{}')",
        ("e-report", "System_Report"),
    )

    title = tool_search._load_current_plaintext_rows(
        conn, ["System_Report"], "Cursor 运行报告", snippet_limit=100,
    )["System_Report"]
    alias = tool_search._load_current_plaintext_rows(
        conn, ["System_Report"], "Cursor Report", snippet_limit=100,
    )["System_Report"]

    assert title["explicit_identity_match"] is True
    assert alias["explicit_identity_match"] is True
