import xml.etree.ElementTree as ET
from pathlib import Path
import pytest

from vector_lake import governance_store, mutation_coordinator
from vector_lake.tool_search import SearchIndexError, format_operational_memory_results, search_vector_lake
from vector_lake.watchdog_app import _watch_directories
from vector_lake.wiki_utils import (
    get_claim_graph_path,
    get_meta_dir,
    get_outbox_signal_path,
    get_raw_dir,
    get_wiki_dir,
)


def test_operational_memory_xml_is_a_well_formed_document(isolated_memory, monkeypatch):
    monkeypatch.setattr(
        governance_store,
        "search_operational_memory",
        lambda *args, **kwargs: [
            {
                "memory_type": "fact'quoted",
                "validity_state": "active&review",
                "retrieval_score": 1.0,
                "text": "A < B & C > D",
                "source_page": "Source_'A&B'.md",
            },
            {
                "memory_type": "decision",
                "validity_state": "active",
                "retrieval_score": 0.5,
                "text": "Second item",
                "source_page": "Source_Second.md",
            },
        ],
    )

    payload = format_operational_memory_results("query", as_xml=True)
    root = ET.fromstring(payload)

    assert root.tag == "MemoryResults"
    assert len(root.findall("Memory_Item")) == 2
    assert root.findall("Memory_Item")[0].text == "A < B & C > D"


def test_signal_and_watch_paths_follow_active_memory_root(isolated_memory):
    assert get_outbox_signal_path() == get_meta_dir() / "outbox_signal.lock"
    assert _watch_directories() == {
        "wiki": get_wiki_dir(),
        "raw": get_raw_dir(),
        "diary": get_raw_dir() / "privacy" / "Diary",
    }

    mutation_coordinator._signal_outbox_consumer()

    assert get_outbox_signal_path().read_text(encoding="utf-8") == "1"


def test_meta_dir_cache_is_keyed_by_active_memory_root(tmp_path, monkeypatch):
    from vector_lake import wiki_utils

    wiki_utils._META_DIR_CACHE = None
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(first_root))
    first_meta = get_meta_dir()
    monkeypatch.setenv("VECTOR_LAKE_MEMORY_DIR", str(second_root))
    second_meta = get_meta_dir()

    assert first_meta == Path(first_root).resolve() / "wiki" / ".meta"
    assert second_meta == Path(second_root).resolve() / "wiki" / ".meta"
    assert second_meta != first_meta


def test_claim_graph_uses_documented_canonical_filename(isolated_memory):
    assert get_claim_graph_path() == get_wiki_dir() / "claim_graph.json"


def test_corrupt_search_index_raises_typed_runtime_error(isolated_memory):
    index_path = get_wiki_dir() / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(SearchIndexError, match="could not be read"):
        search_vector_lake("test")
