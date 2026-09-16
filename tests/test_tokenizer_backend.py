"""Tests for the pluggable tokenizer backend.

Backend chain: ``rjieba`` (jieba-rs via PyO3, preferred) -> ``jieba`` (pure
Python fallback).  The contract that matters:

* the Rust backend wins when importable, and ``lcut`` degrades to ``cut``
  because the binding exposes no ``lcut``,
* a missing optional backend degrades to the pure-Python one instead of
  disabling tokenization,
* ``VECTOR_LAKE_TOKENIZER`` can force either backend, and forcing an unavailable
  one warns and still falls back,
* switching backends invalidates the search-index cache (otherwise two different
  segmentations would be silently mixed inside one FTS index),
* ``add_word`` reports the Rust backend's lack of a dictionary API instead of
  pretending success.
"""
import importlib
import importlib.machinery
import sys
import types

import pytest

from vector_lake import indexer, tokenizer


class _RustBackend(types.ModuleType):
    """Mirrors the real binding: cut/cut_all/cut_for_search/tag/tokenize only."""

    def cut(self, text, hmm=True):
        return [f"rs:{chunk}" for chunk in text.split()]


class _PythonBackend(types.ModuleType):
    """Mirrors jieba: cut plus lcut and a dictionary API."""

    __version__ = "0.42.1"

    def __init__(self, name):
        super().__init__(name)
        self.added = []

    def cut(self, text):
        return [f"py:{chunk}" for chunk in text.split()]

    def lcut(self, text):
        return self.cut(text)

    def add_word(self, term):
        self.added.append(term)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("VECTOR_LAKE_TOKENIZER", raising=False)
    tokenizer.reset_backend_cache()
    yield
    tokenizer.reset_backend_cache()


def _install(monkeypatch, name, module):
    monkeypatch.setitem(sys.modules, name, module)
    tokenizer.reset_backend_cache()
    return module


def _hide(monkeypatch, name):
    """Make ``import name`` raise ImportError, as a missing wheel does."""

    class _RaisingLoader:
        def create_module(self, spec):
            raise ImportError(f"no {name} for this platform")

        def exec_module(self, module):
            raise ImportError(f"no {name} for this platform")

    class _RaisingFinder:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == name:
                return importlib.machinery.ModuleSpec(fullname, _RaisingLoader())
            return None

    monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_RaisingFinder(), *sys.meta_path])
    tokenizer.reset_backend_cache()


# --- backend selection -----------------------------------------------------


def test_prefers_rjieba_when_importable(monkeypatch):
    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))

    assert tokenizer.backend_name() == "rjieba"
    assert not tokenizer.cut("医疗")[0].startswith("py:")
    assert tokenizer.split("医疗") == tokenizer.cut("医疗")


def test_rjieba_version_reports_the_crate_in_force(monkeypatch):
    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))

    assert tokenizer.JIEBA_RS_PINNED in tokenizer.backend_version()
    assert "rjieba" in tokenizer.backend_version()


def test_split_degrades_to_cut_when_lcut_is_missing(monkeypatch):
    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))

    assert tokenizer.split("医疗") == ["rs:医疗"]
    assert not hasattr(sys.modules["rjieba"], "lcut")


def test_falls_back_to_jieba_when_rjieba_missing(monkeypatch):
    _hide(monkeypatch, "rjieba")
    _install(monkeypatch, "jieba", _PythonBackend("jieba"))

    assert tokenizer.backend_name() == "jieba"
    assert tokenizer.cut("医疗") == ["py:医疗"]
    assert tokenizer.tokenize_joined("医疗") == "py:医疗"


def test_falls_back_to_jieba_when_rjieba_fails_to_load(monkeypatch):
    _hide(monkeypatch, "rjieba")
    _install(monkeypatch, "jieba", _PythonBackend("jieba"))

    assert tokenizer.backend_name() == "jieba"
    assert tokenizer.tokenize_joined("医疗") == "py:医疗"


def test_env_override_can_force_pure_python(monkeypatch):
    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))
    _install(monkeypatch, "jieba", _PythonBackend("jieba"))
    monkeypatch.setenv("VECTOR_LAKE_TOKENIZER", "jieba")
    tokenizer.reset_backend_cache()

    assert tokenizer.backend_name() == "jieba"
    assert tokenizer.cut("医疗") == ["py:医疗"]


def test_env_override_can_force_rjieba(monkeypatch):
    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))
    monkeypatch.setenv("VECTOR_LAKE_TOKENIZER", "rjieba")
    tokenizer.reset_backend_cache()

    assert tokenizer.backend_name() == "rjieba"


def test_forced_but_missing_backend_warns_and_falls_back(monkeypatch, caplog):
    """Forcing an unavailable backend must not disable tokenization."""
    _hide(monkeypatch, "rjieba")
    _install(monkeypatch, "jieba", _PythonBackend("jieba"))
    monkeypatch.setenv("VECTOR_LAKE_TOKENIZER", "rjieba")
    tokenizer.reset_backend_cache()

    with caplog.at_level("WARNING", logger="vector-lake-tokenizer"):
        assert tokenizer.backend_name() == "jieba"

    assert [r for r in caplog.records if r.levelname == "WARNING"]
    assert tokenizer.cut("医疗") == ["py:医疗"]


def test_missing_optional_backend_is_logged_at_debug(monkeypatch, caplog):
    _hide(monkeypatch, "rjieba")
    _install(monkeypatch, "jieba", _PythonBackend("jieba"))

    with caplog.at_level("WARNING", logger="vector-lake-tokenizer"):
        assert tokenizer.backend_name() == "jieba"

    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_backend_is_selected_only_once_per_process(monkeypatch, caplog):
    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))

    with caplog.at_level("INFO", logger="vector-lake-tokenizer"):
        for _ in range(5):
            tokenizer.backend_name()
            tokenizer.backend_version()
            tokenizer.supports_add_word()
            tokenizer.cut("医疗")

    selections = [r for r in caplog.records if "jieba-rs" in r.getMessage()]
    assert len(selections) == 1, f"backend selection was logged {len(selections)} times"


def test_unknown_env_value_falls_back_to_auto_selection(monkeypatch):
    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))
    monkeypatch.setenv("VECTOR_LAKE_TOKENIZER", "not-a-backend")
    tokenizer.reset_backend_cache()

    assert tokenizer.backend_name() == "rjieba"


# --- index integration -----------------------------------------------------


def test_tokenize_joined_matches_indexer_contract(monkeypatch):
    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))

    assert indexer._tokenize_for_fts("医疗 数据") == "rs:医疗 rs:数据"
    assert indexer._tokenize_for_fts("") == ""


def test_backend_switch_invalidates_the_search_index_cache(monkeypatch):
    node = {"title": "T", "summary": "S", "aliases": ["a"]}

    _hide(monkeypatch, "rjieba")
    _install(monkeypatch, "jieba", _PythonBackend("jieba"))
    pure = indexer._node_content_digest(node, "body")

    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))
    rust = indexer._node_content_digest(node, "body")

    assert pure != rust, "switching tokenizers must force re-tokenization"


# --- add_word capability reporting ----------------------------------------


def test_add_word_is_unsupported_on_the_rust_backend(monkeypatch, caplog):
    _install(monkeypatch, "rjieba", _RustBackend("rjieba"))

    with caplog.at_level("WARNING", logger="vector-lake-tokenizer"):
        assert tokenizer.supports_add_word() is False
        assert tokenizer.add_word("电子病历") is False

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, "the limitation must be reported once, not per term"


def test_add_word_works_on_the_python_backend(monkeypatch):
    _hide(monkeypatch, "rjieba")
    _install(monkeypatch, "jieba", _PythonBackend("jieba"))

    assert tokenizer.supports_add_word() is True
    assert tokenizer.add_word("电子病历") is True
    assert sys.modules["jieba"].added == ["电子病历"]
    assert tokenizer.add_word("") is False


# --- source-level guard ----------------------------------------------------


def test_no_module_imports_a_tokenizer_directly():
    """All tokenization must go through vector_lake.tokenizer."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "vector_lake"
    forbidden = ("jieba", "jieba_fast", "rjieba")
    offenders = []
    for path in root.glob("*.py"):
        if path.name == "tokenizer.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            for name in forbidden:
                if stripped.startswith(f"import {name}") or stripped.startswith(f"from {name} "):
                    offenders.append(f"{path.name}:{lineno}: {stripped}")
    assert not offenders, f"import the tokenizer module instead: {offenders}"


# --- real backend smoke (only when installed) -----------------------------


@pytest.mark.skipif(
    importlib.util.find_spec("rjieba") is None, reason="rjieba is not installed"
)
def test_real_rjieba_backend_is_usable():
    import os
    import subprocess

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from vector_lake import tokenizer;"
            "print(tokenizer.backend_name());"
            "print(' '.join(tokenizer.cut('医疗数据平台与电子病历系统')))",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        check=True,
    )
    out = result.stdout.splitlines()
    assert out[0] == "rjieba"
    assert "医疗" in out[1] and "电子" in out[1]
