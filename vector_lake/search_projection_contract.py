"""Pure, deterministic contracts shared by search and embedding projections."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any


EMBEDDING_INPUT_CONTRACT = "vector-lake-embedding-input/v1"
CANONICAL_PROJECTION_SURFACES = (
    "entities",
    "claims",
    "sources",
    "page_graph_edges",
    "claim_graph_edges",
)


def normalize_runtime_generations(value: Any) -> dict[str, int] | None:
    """Return only the exact canonical projection-generation contract."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, Mapping):
        return None
    if set(value) != set(CANONICAL_PROJECTION_SURFACES):
        return None
    normalized: dict[str, int] = {}
    for surface in CANONICAL_PROJECTION_SURFACES:
        generation = value.get(surface)
        if isinstance(generation, bool) or not isinstance(generation, int):
            return None
        if generation < 0:
            return None
        normalized[surface] = generation
    return normalized


def verified_projection_runtime_generations(
    index_data: Mapping[str, Any],
) -> dict[str, int] | None:
    """Extract the exact verified generation binding from an index snapshot."""
    manifest = index_data.get("projection_manifest")
    if not isinstance(manifest, Mapping):
        return None
    binding = manifest.get("canonical_generation")
    if not isinstance(binding, Mapping):
        return None
    if not (
        binding.get("verified") is True
        or str(binding.get("status") or "").casefold() == "verified"
    ):
        return None
    return normalize_runtime_generations(binding.get("runtime_generations"))


def embedding_text_for_node(node: Mapping[str, Any], max_chars: int = 15_000) -> str:
    aliases = node.get("aliases") or []
    aliases_text = (
        " ".join(str(item) for item in aliases)
        if isinstance(aliases, list)
        else str(aliases)
    )
    text = " ".join(
        str(part or "")
        for part in (
            node.get("title"),
            aliases_text,
            node.get("summary"),
            node.get("raw_text"),
        )
    )
    return re.sub(r"\s+", " ", text).strip()[: max(1, int(max_chars))]


def embedding_content_sha256(
    node: Mapping[str, Any],
    max_chars: int = 15_000,
) -> str:
    text = embedding_text_for_node(node, max_chars=max_chars)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fts_corpus_sha256(rows: Iterable[tuple[str, str, str, str]]) -> str:
    """Hash a key-ordered FTS corpus without ambiguous field boundaries."""
    digest = hashlib.sha256()
    normalized = sorted(
        (str(key), str(title), str(summary), str(text))
        for key, title, summary, text in rows
    )
    for row in normalized:
        digest.update(encode_fts_corpus_row(row))
    return digest.hexdigest()


def fts_corpus_sha256_ordered(
    rows: Iterable[tuple[str, str, str, str]],
) -> str:
    """Stream a strictly key-ordered corpus without a second O(N) list."""
    digest = hashlib.sha256()
    previous_key: str | None = None
    for key, title, summary, text in rows:
        row = (str(key), str(title), str(summary), str(text))
        if previous_key is not None and row[0] <= previous_key:
            raise ValueError("FTS corpus rows must be strictly key ordered")
        digest.update(encode_fts_corpus_row(row))
        previous_key = row[0]
    return digest.hexdigest()


def encode_fts_corpus_row(row: Iterable[Any]) -> bytes:
    """Encode one FTS row for ordered, streaming corpus verification."""
    normalized = tuple(str(value) for value in row)
    if len(normalized) != 4:
        raise ValueError("FTS corpus rows must contain exactly four fields")
    return (
        json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
