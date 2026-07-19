"""Shared Chinese tokenization boundary for indexing and querying."""

from __future__ import annotations

import importlib
from functools import lru_cache


@lru_cache(maxsize=1)
def load_tokenizer():
    """Load the maintained jieba-rs Python binding."""
    return importlib.import_module("rjieba")


def segment_text(text: str) -> list[str]:
    if not text:
        return []
    return [str(token) for token in load_tokenizer().cut(text) if str(token).strip()]


def tokenize_for_fts(text: str) -> str:
    return " ".join(segment_text(text))
