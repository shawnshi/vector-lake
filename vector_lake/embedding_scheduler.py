"""Rate-aware Gemini embedding scheduler.

The scheduler treats embeddings as a resumable projection:
- never clear existing vectors just because an index rebuild starts;
- embed only missing nodes by default;
- batch by conservative token estimates;
- enforce RPM/TPM windows before requests;
- back off on provider quota errors without corrupting existing vectors.
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, replace
from collections.abc import Iterable, Iterator
from typing import Any

from filelock import FileLock, Timeout

from vector_lake import db_store
from vector_lake.wiki_utils import get_meta_dir


DEFAULT_MODEL = "gemini-embedding-2"
DEFAULT_RPM = 3000
DEFAULT_TPM = 1_000_000
DEFAULT_DIMENSION = 3072


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.01, min(1.0, float(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class EmbeddingRateConfig:
    model: str = DEFAULT_MODEL
    rpm: int = DEFAULT_RPM
    tpm: int = DEFAULT_TPM
    utilization: float = 0.80
    max_batch_items: int = 100
    max_batch_tokens: int = 200_000
    max_chars_per_item: int = 15_000
    max_retries: int = 5
    dimension: int = DEFAULT_DIMENSION
    max_consecutive_failed_batches: int = 3

    @property
    def effective_rpm(self) -> int:
        return max(1, int(self.rpm * self.utilization))

    @property
    def effective_tpm(self) -> int:
        return max(1, int(self.tpm * self.utilization))


def load_embedding_rate_config() -> EmbeddingRateConfig:
    return EmbeddingRateConfig(
        model=os.environ.get("VECTOR_LAKE_EMBEDDING_MODEL", DEFAULT_MODEL),
        rpm=_env_int("VECTOR_LAKE_EMBEDDING_RPM", DEFAULT_RPM),
        tpm=_env_int("VECTOR_LAKE_EMBEDDING_TPM", DEFAULT_TPM),
        utilization=_env_float("VECTOR_LAKE_EMBEDDING_UTILIZATION", 0.80),
        max_batch_items=_env_int("VECTOR_LAKE_EMBEDDING_MAX_BATCH_ITEMS", 100),
        max_batch_tokens=_env_int("VECTOR_LAKE_EMBEDDING_MAX_BATCH_TOKENS", 200_000),
        max_chars_per_item=_env_int("VECTOR_LAKE_EMBEDDING_MAX_CHARS_PER_ITEM", 15_000),
        max_retries=_env_int("VECTOR_LAKE_EMBEDDING_MAX_RETRIES", 5),
        dimension=_env_int("VECTOR_LAKE_EMBEDDING_DIMENSION", DEFAULT_DIMENSION),
        max_consecutive_failed_batches=_env_int("VECTOR_LAKE_EMBEDDING_MAX_CONSECUTIVE_FAILURES", 3),
    )


def embedding_text_for_node(node: dict[str, Any], max_chars: int = 15_000) -> str:
    aliases = node.get("aliases") or []
    aliases_text = " ".join(str(item) for item in aliases) if isinstance(aliases, list) else str(aliases)
    text = " ".join(
        str(part or "")
        for part in [node.get("title"), aliases_text, node.get("summary"), node.get("raw_text")]
    )
    return re.sub(r"\s+", " ", text).strip()[:max_chars]


def estimate_embedding_tokens(text: str) -> int:
    """Conservative tokenizer-free estimate for mixed Chinese/English content."""
    if not text:
        return 1
    cjk_chars = len(re.findall(r"[\u3400-\u9fff]", text))
    non_cjk = re.sub(r"[\u3400-\u9fff]", "", text)
    latin_words = len(re.findall(r"[A-Za-z0-9_]+", non_cjk))
    residual_chars = len(re.sub(r"[A-Za-z0-9_\s]", "", non_cjk))
    return max(1, int(cjk_chars + math.ceil(latin_words * 1.3) + math.ceil(residual_chars / 2)))


class EmbeddingInventoryUnavailable(RuntimeError):
    """Raised when vector coverage cannot be established safely."""


def existing_embedding_ids(
    *,
    read_only: bool = False,
    allow_missing_database: bool = False,
) -> set[str]:
    conn = None
    try:
        if read_only:
            path = db_store.peek_db_path()
            if not path.is_file():
                if allow_missing_database:
                    return set()
                raise FileNotFoundError(path)
            conn = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=5.0,
            )
            conn.row_factory = sqlite3.Row
            db_store._load_sqlite_vec_extension(conn)
            conn.execute("PRAGMA query_only=ON")
        else:
            conn = db_store.get_vector_connection()
        return {
            row["entity_id"]
            for row in conn.execute("SELECT entity_id FROM vec_embeddings")
        }
    except Exception as exc:
        raise EmbeddingInventoryUnavailable(
            "Cannot read the existing embedding inventory; refusing to treat "
            "coverage as empty because that could trigger duplicate provider calls."
        ) from exc
    finally:
        if read_only and conn is not None:
            conn.close()


def embedding_coverage(
    index_data: dict[str, Any],
    existing: set[str] | None = None,
) -> dict[str, int]:
    existing = existing_embedding_ids() if existing is None else existing
    node_keys = (index_data.get("nodes") or {}).keys()
    node_count = 0
    embedded = 0
    for node_key in node_keys:
        node_count += 1
        if node_key in existing:
            embedded += 1
    return {
        "nodes": node_count,
        "embedded": embedded,
        "missing": node_count - embedded,
        "stale": len(existing) - embedded,
    }


def _candidate_items(
    index_data: dict[str, Any],
    *,
    include_existing: bool = False,
    limit: int | None = None,
    config: EmbeddingRateConfig | None = None,
    existing: set[str] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield deterministic candidates without retaining all candidate text."""
    config = config or load_embedding_rate_config()
    embedded_ids = (
        set()
        if include_existing
        else (existing_embedding_ids() if existing is None else existing)
    )
    selected = 0
    nodes = index_data.get("nodes") or {}
    for node_key in sorted(nodes):
        if node_key in embedded_ids:
            continue
        node = nodes[node_key]
        text = embedding_text_for_node(node, max_chars=config.max_chars_per_item)
        if not text:
            continue
        yield {
            "node_key": node_key,
            "text": text,
            "tokens": estimate_embedding_tokens(text),
        }
        selected += 1
        if limit is not None and selected >= max(1, int(limit)):
            break


def _batch_items(
    items: Iterable[dict[str, Any]],
    config: EmbeddingRateConfig,
) -> Iterator[list[dict[str, Any]]]:
    """Yield one bounded request batch at a time."""
    current: list[dict[str, Any]] = []
    current_tokens = 0
    batch_token_cap = max(1, min(config.max_batch_tokens, config.effective_tpm))
    for item in items:
        item_tokens = max(1, int(item["tokens"]))
        if current and (
            len(current) >= config.max_batch_items
            or current_tokens + item_tokens > batch_token_cap
        ):
            yield current
            current = []
            current_tokens = 0
        current.append(item)
        current_tokens += item_tokens
    if current:
        yield current


def _candidate_plan(
    index_data: dict[str, Any],
    *,
    include_existing: bool,
    limit: int | None,
    config: EmbeddingRateConfig,
    existing: set[str],
) -> tuple[int, int, int]:
    """Count a plan with bounded memory, discarding each batch after use."""
    candidates = 0
    estimated_tokens = 0
    estimated_requests = 0
    items = _candidate_items(
        index_data,
        include_existing=include_existing,
        limit=limit,
        config=config,
        existing=existing,
    )
    for batch in _batch_items(items, config):
        candidates += len(batch)
        estimated_tokens += sum(int(item["tokens"]) for item in batch)
        estimated_requests += 1
        del batch
    return candidates, estimated_tokens, estimated_requests


class EmbeddingRateLimitTimeout(TimeoutError):
    """Raised when an interactive request cannot reserve quota promptly."""


class MinuteRateLimiter:
    """Cross-process rolling-window limiter backed by canonical SQLite."""

    def __init__(self, config: EmbeddingRateConfig):
        self.config = config

    def reserve(
        self,
        request_tokens: int,
        max_wait_seconds: float | None = None,
    ):
        request_tokens = max(1, int(request_tokens))
        deadline = (
            None
            if max_wait_seconds is None
            else time.monotonic() + max(0.0, float(max_wait_seconds))
        )

        def remaining_wait() -> float | None:
            if deadline is None:
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EmbeddingRateLimitTimeout(
                    "Embedding quota is busy; interactive request fell back "
                    "to lexical search."
                )
            return remaining
        if request_tokens > self.config.effective_tpm:
            raise ValueError(
                f"Embedding request tokens {request_tokens} exceed effective TPM {self.config.effective_tpm}"
            )
        remaining_wait()
        db_store.init_db()
        remaining_wait()
        while True:
            now = time.time()
            cutoff = now - 60.0
            wait_seconds = 0.0
            with db_store.transaction(max_wait_seconds=remaining_wait()):
                remaining_wait()
                conn = db_store.get_connection()
                conn.execute(
                    "DELETE FROM embedding_rate_reservations WHERE reserved_at <= ?",
                    (cutoff,),
                )
                row = conn.execute(
                    "SELECT COUNT(*) AS requests, COALESCE(SUM(token_count), 0) AS tokens, "
                    "MIN(reserved_at) AS oldest FROM embedding_rate_reservations"
                ).fetchone()
                requests = int(row["requests"] or 0)
                tokens = int(row["tokens"] or 0)
                if (
                    requests + 1 <= self.config.effective_rpm
                    and tokens + request_tokens <= self.config.effective_tpm
                ):
                    remaining_wait()
                    conn.execute(
                        "INSERT INTO embedding_rate_reservations "
                        "(reservation_id, reserved_at, token_count) VALUES (?, ?, ?)",
                        (uuid.uuid4().hex, now, request_tokens),
                    )
                    remaining_wait()
                    return
                oldest = float(row["oldest"] or now)
                wait_seconds = max(0.01, oldest + 60.0 - now + 0.05)
            remaining = remaining_wait()
            if remaining is not None and wait_seconds > remaining:
                raise EmbeddingRateLimitTimeout(
                    "Embedding quota is busy; interactive request fell back "
                    "to lexical search."
                )
            time.sleep(wait_seconds)


class EmbeddingResponseError(RuntimeError):
    """Raised when the provider response cannot be safely mapped to inputs."""


def _provider_contents(contents: list[str]) -> list[Any]:
    from google.genai import types

    return [
        types.UserContent(parts=[types.Part.from_text(text=str(content))])
        for content in contents
    ]


def _create_client(timeout_ms: int | None = None):
    from google import genai
    from google.genai import types

    if timeout_ms is None:
        timeout_ms = _env_int("VECTOR_LAKE_EMBEDDING_TIMEOUT_MS", 30_000)
    timeout_ms = max(1, int(timeout_ms))
    return genai.Client(http_options=types.HttpOptions(timeout=timeout_ms))


def _validated_response_values(response: Any, expected_count: int, dimension: int) -> list[list[float]]:
    embeddings = list(getattr(response, "embeddings", []) or [])
    if len(embeddings) != expected_count:
        raise EmbeddingResponseError(
            f"Embedding response count mismatch: expected {expected_count}, got {len(embeddings)}"
        )
    values_list: list[list[float]] = []
    for position, embedding in enumerate(embeddings):
        values = list(getattr(embedding, "values", None) or [])
        if len(values) != dimension:
            raise EmbeddingResponseError(
                f"Embedding dimension mismatch at position {position}: expected {dimension}, got {len(values)}"
            )
        values_list.append(values)
    return values_list


def _request_embeddings(
    client: Any,
    contents: list[str],
    request_tokens: int,
    config: EmbeddingRateConfig,
    limiter: MinuteRateLimiter,
    max_wait_seconds: float | None = None,
) -> list[list[float]]:
    last_error: Exception | None = None
    provider_contents = _provider_contents(contents)
    for attempt in range(config.max_retries + 1):
        try:
            if max_wait_seconds is None:
                limiter.reserve(request_tokens)
            else:
                limiter.reserve(request_tokens, max_wait_seconds=max_wait_seconds)
            response = client.models.embed_content(model=config.model, contents=provider_contents)
            return _validated_response_values(response, len(contents), config.dimension)
        except Exception as exc:
            last_error = exc
            if attempt >= config.max_retries:
                break
            message = str(exc)
            is_quota = "429" in message or "RESOURCE_EXHAUSTED" in message or "quota" in message.lower()
            time.sleep(60.0 if is_quota else min(60.0, 2.0 ** attempt))
    assert last_error is not None
    raise last_error


def embed_texts(
    texts: list[str],
    *,
    max_retries: int | None = None,
    timeout_ms: int | None = None,
    max_wait_seconds: float | None = None,
) -> list[list[float]]:
    """Shared validated embedding entrypoint for small runtime requests."""
    if not texts or not os.environ.get("GEMINI_API_KEY"):
        return []
    config = load_embedding_rate_config()
    if max_retries is not None:
        config = replace(config, max_retries=max(0, int(max_retries)))
    normalized = [str(text)[:config.max_chars_per_item] for text in texts]
    tokens = sum(estimate_embedding_tokens(text) for text in normalized)
    client = (
        _create_client()
        if timeout_ms is None
        else _create_client(timeout_ms=timeout_ms)
    )
    try:
        return _request_embeddings(
            client,
            normalized,
            tokens,
            config,
            MinuteRateLimiter(config),
            max_wait_seconds=max_wait_seconds,
        )
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def embedding_backfill(index_data: dict[str, Any], dry_run: bool = True, limit: int | None = None, include_existing: bool = False) -> dict[str, Any]:
    """Backfill missing vector embeddings under Gemini RPM/TPM limits."""
    config = load_embedding_rate_config()
    inventory_state = "ready"
    if dry_run:
        inventory_path = db_store.peek_db_path()
        inventory_missing = not inventory_path.is_file()
        existing = existing_embedding_ids(
            read_only=True,
            allow_missing_database=True,
        )
        if inventory_missing:
            inventory_state = "database_missing"
    else:
        existing = existing_embedding_ids()
    coverage_before = embedding_coverage(index_data, existing=existing)
    candidates, estimated_tokens, estimated_requests = _candidate_plan(
        index_data,
        include_existing=include_existing,
        limit=limit,
        config=config,
        existing=existing,
    )
    plan = {
        "dry_run": dry_run,
        "model": config.model,
        "rpm": config.rpm,
        "tpm": config.tpm,
        "utilization": config.utilization,
        "effective_rpm": config.effective_rpm,
        "effective_tpm": config.effective_tpm,
        "candidates": candidates,
        "estimated_tokens": estimated_tokens,
        "estimated_requests": estimated_requests,
        "coverage_before": coverage_before,
        "inventory_state": inventory_state,
        "embedded": 0,
        "failed_batches": 0,
    }
    if inventory_state == "database_missing":
        plan["preview_warning"] = (
            "canonical embedding database is missing; preview assumes no existing vectors"
        )
    if dry_run or not candidates:
        return plan
    if not os.environ.get("GEMINI_API_KEY"):
        plan["skipped"] = "GEMINI_API_KEY not set"
        return plan

    lock = FileLock(str(get_meta_dir() / ".embedding-backfill.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        plan["skipped"] = "another embedding backfill is already running"
        return plan

    try:
        existing = existing_embedding_ids()
        coverage_before = embedding_coverage(index_data, existing=existing)
        candidates, estimated_tokens, estimated_requests = _candidate_plan(
            index_data,
            include_existing=include_existing,
            limit=limit,
            config=config,
            existing=existing,
        )
    except BaseException:
        lock.release()
        raise
    plan.update(
        {
            "candidates": candidates,
            "estimated_tokens": estimated_tokens,
            "estimated_requests": estimated_requests,
            "coverage_before": coverage_before,
        }
    )
    if not candidates:
        plan["skipped"] = "no missing embeddings after lock acquisition"
        lock.release()
        return plan

    run_id = uuid.uuid4().hex
    plan["run_id"] = run_id
    db_store.start_embedding_run(run_id, config.model, candidates)
    last_error = ""
    consecutive_failures = 0
    client = None
    try:
        client = _create_client()
        limiter = MinuteRateLimiter(config)
        items = _candidate_items(
            index_data,
            include_existing=include_existing,
            limit=limit,
            config=config,
            existing=existing,
        )
        for batch in _batch_items(items, config):
            contents = [item["text"] for item in batch]
            batch_tokens = sum(int(item["tokens"]) for item in batch)
            values_list = None
            try:
                values_list = _request_embeddings(client, contents, batch_tokens, config, limiter)
                for item, values in zip(batch, values_list, strict=True):
                    db_store.upsert_embedding(item["node_key"], values)
                    plan["embedded"] += 1
                consecutive_failures = 0
            except Exception as exc:
                last_error = str(exc)[:500]
                plan["failed_batches"] += 1
                plan["last_error"] = last_error
                consecutive_failures += 1
            finally:
                values_list = None
            del contents, batch
            db_store.update_embedding_run(
                run_id,
                plan["embedded"],
                plan["failed_batches"],
                last_error,
            )
            if consecutive_failures >= config.max_consecutive_failed_batches:
                plan["stopped"] = "consecutive batch failure guard reached"
                break
        status = "completed" if not plan["failed_batches"] else ("partial" if plan["embedded"] else "failed")
        db_store.finish_embedding_run(
            run_id,
            status,
            plan["embedded"],
            plan["failed_batches"],
            last_error,
        )
        plan["coverage_after"] = embedding_coverage(index_data)
        return plan
    except Exception as exc:
        last_error = str(exc)[:500]
        db_store.finish_embedding_run(run_id, "failed", plan["embedded"], plan["failed_batches"], last_error)
        raise
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        lock.release()
