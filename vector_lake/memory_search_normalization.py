"""Unicode normalization contract for operational-memory search."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache


NORMALIZATION_CONTRACT_DOMAIN = "operational-memory-search:casefold:v1"
BUILD_MARKER = hashlib.sha256(
    (NORMALIZATION_CONTRACT_DOMAIN + ":building").encode("utf-8")
).hexdigest()

_MAX_CACHE_ENTRIES = 64
_MAX_TERMS = 128
_MAX_TERM_CHARS = 512
_MAX_ENCODED_CHARS = _MAX_TERMS * (_MAX_TERM_CHARS + 3) + 2


def casefold_text(value: object) -> str:
    """Preserve existing string/default coercion while applying casefold only."""
    return str(value or "").casefold()


@lru_cache(maxsize=_MAX_CACHE_ENTRIES)
def _decode_terms(encoded_terms: str) -> tuple[str, ...]:
    if not isinstance(encoded_terms, str):
        raise ValueError("encoded search terms must be a JSON string")
    if len(encoded_terms) > _MAX_ENCODED_CHARS:
        raise ValueError("encoded search terms exceed the bounded payload limit")
    decoded = json.loads(encoded_terms)
    if not isinstance(decoded, list) or len(decoded) > _MAX_TERMS:
        raise ValueError("encoded search terms must contain at most 128 terms")
    if not all(isinstance(term, str) for term in decoded):
        raise ValueError("encoded search terms must contain only strings")
    if any(len(term) > _MAX_TERM_CHARS for term in decoded):
        raise ValueError("encoded search terms contain a term longer than 512 characters")
    return tuple(decoded)


def casefold_any(search_text: object, encoded_terms: object) -> int:
    """SQLite UDF: true when any encoded term occurs in case-folded text."""
    haystack = casefold_text(search_text)
    return int(any(term in haystack for term in _decode_terms(encoded_terms)))


def register_sqlite_functions(conn: object) -> None:
    conn.create_function("casefold_any", 2, casefold_any, deterministic=True)
    conn.create_function("casefold_text", 1, casefold_text, deterministic=True)
