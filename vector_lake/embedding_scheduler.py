"""Rate-aware Gemini embedding scheduler.

The scheduler treats embeddings as a resumable projection:
- never clear existing vectors just because an index rebuild starts;
- embed only missing nodes by default;
- batch by conservative token estimates;
- enforce RPM/TPM windows before requests;
- back off on provider quota errors without corrupting existing vectors.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import uuid
import logging
from dataclasses import dataclass
from typing import Any, Callable

from filelock import FileLock, Timeout

from vector_lake import db_store
from vector_lake.wiki_utils import get_meta_dir

log = logging.getLogger(__name__)


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
    max_tokens_per_item: int = 7_500
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
        max_tokens_per_item=_env_int("VECTOR_LAKE_EMBEDDING_MAX_TOKENS_PER_ITEM", 7_500),
        max_retries=_env_int("VECTOR_LAKE_EMBEDDING_MAX_RETRIES", 5),
        dimension=_env_int("VECTOR_LAKE_EMBEDDING_DIMENSION", DEFAULT_DIMENSION),
        max_consecutive_failed_batches=_env_int("VECTOR_LAKE_EMBEDDING_MAX_CONSECUTIVE_FAILURES", 3),
    )


def embedding_text_for_node(node: dict[str, Any], max_chars: int = 15_000, body_text: str | None = None) -> str:
    """Embedding input text.  ``body_text`` supplies the page body, which is no
    longer carried in index.json."""
    aliases = node.get("aliases") or []
    aliases_text = " ".join(str(item) for item in aliases) if isinstance(aliases, list) else str(aliases)
    body = body_text if body_text is not None else node.get("raw_text")
    text = " ".join(
        str(part or "")
        for part in [node.get("title"), aliases_text, node.get("summary"), body]
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


def clamp_to_token_budget(text: str, max_tokens: int) -> tuple[str, int]:
    """Trim one item so a single embedding request cannot exceed the model window.

    The character cap alone was unsafe for Chinese, where one character is
    roughly one token.
    """
    tokens = estimate_embedding_tokens(text)
    if tokens <= max_tokens:
        return text, tokens
    ratio = len(text) / max(1, tokens)
    truncated = text[: max(200, int(max_tokens * ratio))]
    while estimate_embedding_tokens(truncated) > max_tokens and len(truncated) > 200:
        truncated = truncated[: int(len(truncated) * 0.9)]
    return truncated, estimate_embedding_tokens(truncated)


def existing_embedding_ids() -> set[str]:
    """Node keys with a stored vector.  Raises if the vector table is unreadable:
    silently returning an empty set turned a broken table into "everything is
    missing" and triggered a full re-embed."""
    conn = db_store.get_connection()
    return {row["entity_id"] for row in conn.execute("SELECT entity_id FROM vec_embeddings")}


def page_bodies_for_keys(keys: list[str] | tuple[str, ...]) -> dict[str, str]:
    """Canonical ``raw_text`` bodies for the given page keys, read in one statement per chunk.

    ``index.json`` no longer carries page bodies, so a candidate's text has to come from
    SQLite.  Loading every entity body to embed a handful of nodes is what made an automatic
    backfill unaffordable: the periodic sweep runs every 15 minutes, and the corpus is now
    7 974 entities.  Chunked ``IN`` lists keep the statement inside SQLite's variable limit
    while staying a bounded number of round trips.
    """
    wanted = [str(key) for key in keys if key]
    if not wanted:
        return {}
    bodies: dict[str, str] = {}
    conn = db_store.get_connection()
    for start in range(0, len(wanted), 400):
        chunk = wanted[start:start + 400]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT f_page_key AS page_key, data_json FROM entities "
            f"WHERE f_page_key IN ({placeholders})",
            chunk,
        )
        for row in rows:
            try:
                bodies[row["page_key"]] = str(json.loads(row["data_json"]).get("raw_text") or "")
            except (json.JSONDecodeError, TypeError):
                continue
    return bodies


def embedding_coverage(index_data: dict[str, Any]) -> dict[str, int]:
    node_keys = set((index_data.get("nodes") or {}).keys())
    existing = existing_embedding_ids()
    return {
        "nodes": len(node_keys),
        "embedded": len(node_keys & existing),
        "missing": len(node_keys - existing),
        "stale": len(existing - node_keys),
    }


def _candidate_items(
    index_data: dict[str, Any],
    *,
    include_existing: bool = False,
    limit: int | None = None,
    config: EmbeddingRateConfig | None = None,
    bodies: dict[str, str] | None = None,
    body_loader: Callable[[list[str]], dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Nodes that still need a vector, in a stable key order.

    ``body_loader`` is the bounded path: a caller that cannot afford to materialise all
    7 974 page bodies -- the periodic sweep -- hands in a loader and only the keys about to
    be embedded are read.  Passing ``bodies`` keeps the whole-corpus behaviour for callers
    that already hold them (the CLI/MCP backfill).
    """
    config = config or load_embedding_rate_config()
    nodes = index_data.get("nodes") or {}
    existing = existing_embedding_ids() if not include_existing else set()
    pending = [key for key in sorted(nodes) if key not in existing]
    if limit is not None:
        pending = pending[: max(1, int(limit))]
    if body_loader is not None and bodies is None:
        bodies = body_loader(pending)
    bodies = bodies or {}
    items: list[dict[str, Any]] = []
    for node_key in pending:
        text = embedding_text_for_node(
            nodes[node_key], max_chars=config.max_chars_per_item, body_text=bodies.get(node_key)
        )
        text, tokens = clamp_to_token_budget(text, config.max_tokens_per_item)
        if not text:
            continue
        items.append({"node_key": node_key, "text": text, "tokens": tokens})
    if limit is not None:
        items = items[: max(1, int(limit))]
    return items


def _batch_items(items: list[dict[str, Any]], config: EmbeddingRateConfig) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_tokens = 0
    batch_token_cap = max(1, min(config.max_batch_tokens, config.effective_tpm))
    for item in items:
        item_tokens = max(1, int(item["tokens"]))
        if current and (
            len(current) >= config.max_batch_items
            or current_tokens + item_tokens > batch_token_cap
        ):
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(item)
        current_tokens += item_tokens
    if current:
        batches.append(current)
    return batches


class MinuteRateLimiter:
    """Cross-process rolling-window limiter backed by canonical SQLite."""

    def __init__(self, config: EmbeddingRateConfig):
        self.config = config

    def reserve(self, request_tokens: int, deadline: float | None = None, durable: bool = True):
        """Claim a slot in the rolling window.

        ``deadline`` is a ``time.monotonic()`` instant.  Without one the limiter
        waits as long as the window requires, which is right for batch backfills;
        with one, an unaffordable wait raises :class:`EmbeddingBudgetExceeded`
        rather than sleeping past the caller's own deadline.

        ``durable=False`` makes the check read-only, for requests that run on a
        read path.  A durable reservation needs ``BEGIN IMMEDIATE``, so recording
        every query embedding put the write lock back on a path that was just
        cleaned of it -- a query would queue behind whatever ingest was writing.
        The cost is that interactive requests are not counted, so the window can be
        exceeded by however many of them land inside it.  That is a deliberate
        trade at the observed scale (a handful of queries per minute against
        ``rpm`` 3000), and the provider's own 429 is now bounded by the caller's
        budget instead of a flat 60 s sleep.
        """
        request_tokens = max(1, int(request_tokens))
        if request_tokens > self.config.effective_tpm:
            raise ValueError(
                f"Embedding request tokens {request_tokens} exceed effective TPM {self.config.effective_tpm}"
            )
        db_store.init_db()
        while True:
            now = time.time()
            cutoff = now - 60.0
            wait_seconds = 0.0
            conn = db_store.get_connection()
            if durable:
                with db_store.transaction():
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
                        conn.execute(
                            "INSERT INTO embedding_rate_reservations "
                            "(reservation_id, reserved_at, token_count) VALUES (?, ?, ?)",
                            (uuid.uuid4().hex, now, request_tokens),
                        )
                        return
                    oldest = float(row["oldest"] or now)
                    wait_seconds = max(0.01, oldest + 60.0 - now + 0.05)
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS requests, COALESCE(SUM(token_count), 0) AS tokens, "
                    "MIN(reserved_at) AS oldest FROM embedding_rate_reservations "
                    "WHERE reserved_at > ?",
                    (cutoff,),
                ).fetchone()
                requests = int(row["requests"] or 0)
                tokens = int(row["tokens"] or 0)
                if (
                    requests + 1 <= self.config.effective_rpm
                    and tokens + request_tokens <= self.config.effective_tpm
                ):
                    return
                oldest = float(row["oldest"] or now)
                wait_seconds = max(0.01, oldest + 60.0 - now + 0.05)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if wait_seconds > remaining:
                    raise EmbeddingBudgetExceeded(
                        f"rate-limit window would hold this request for {wait_seconds:.1f}s "
                        f"but only {max(0.0, remaining):.1f}s of the caller's budget remains"
                    )
            time.sleep(wait_seconds)


class EmbeddingResponseError(RuntimeError):
    """Raised when the provider response cannot be safely mapped to inputs."""


class EmbeddingBudgetExceeded(RuntimeError):
    """The caller's deadline left no room for the wait this request needs.

    Distinct from a provider error: nothing was sent, or the retry the provider
    asked for was refused.  Callers that hold a deadline of their own (the query
    path runs inside an MCP tool call) degrade on this instead of parking.
    """


EMBEDDING_TRANSPORT_ENV = "VECTOR_LAKE_EMBEDDING_TRANSPORT"
REST_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"


def embedding_transport() -> str:
    """``"rest"`` (default) or ``"sdk"``.

    The SDK path costs ``import google.genai`` (3.5 s) plus ``genai.Client()`` (1.3 s) in every
    fresh process -- measured, and 80 % of what looked like "embedding latency" on the first
    query of a server or the whole of a CLI search.  The REST call is the same endpoint the SDK
    posts to, so the default avoids that import entirely and the SDK stays available as the
    proven fallback (``VECTOR_LAKE_EMBEDDING_TRANSPORT=sdk``, or an unexpected REST failure).
    """
    value = str(os.environ.get(EMBEDDING_TRANSPORT_ENV, "rest") or "rest").strip().lower()
    return value if value in {"rest", "sdk"} else "rest"


def _rest_embed_contents(contents: list[str], config) -> list[list[float]]:
    """One ``batchEmbedContents`` POST, with the SDK's error semantics.

    Raises on any non-200 so the caller's retry/budget loop behaves exactly as it did with the
    SDK: the loop classifies a quota error from the message text, and a 429 body carries it.
    """
    import json as _json
    import urllib.error
    import urllib.request

    api_key = str(os.environ.get("GEMINI_API_KEY") or "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    timeout_ms = _env_int("VECTOR_LAKE_EMBEDDING_TIMEOUT_MS", 30_000)
    payload = _json.dumps(
        {
            "requests": [
                {"model": f"models/{config.model}", "content": {"parts": [{"text": str(text)}]}}
                for text in contents
            ]
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        REST_ENDPOINT.format(model=config.model),
        data=payload,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, timeout_ms / 1000.0)) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001 - the status is the useful part
            pass
        raise RuntimeError(f"HTTP {exc.code} from the embedding endpoint: {detail}") from exc
    payload_json = _json.loads(body.decode("utf-8"))
    embeddings = payload_json.get("embeddings") or []
    values_list = [list(item.get("values") or []) for item in embeddings]
    if len(values_list) != len(contents):
        raise EmbeddingResponseError(
            f"Embedding response count mismatch: expected {len(contents)}, got {len(values_list)}"
        )
    for index, values in enumerate(values_list):
        if len(values) != config.dimension:
            raise EmbeddingResponseError(
                f"Embedding dimension mismatch at position {index}: "
                f"expected {config.dimension}, got {len(values)}"
            )
    return values_list


def _provider_contents(contents: list[str]) -> list[Any]:
    from google.genai import types

    return [
        types.UserContent(parts=[types.Part.from_text(text=str(content))])
        for content in contents
    ]


def _create_client():
    from google import genai
    from google.genai import types

    timeout_ms = _env_int("VECTOR_LAKE_EMBEDDING_TIMEOUT_MS", 30_000)
    return genai.Client(http_options=types.HttpOptions(timeout=timeout_ms))


# ``_create_client()`` measured 1.7-6.9 s per call on the operator machine (the
# ``google.genai`` import plus auth/transport setup), and it was invoked for
# every embedding request -- including the single-vector query embedding on the
# search hot path, where it dominated the 2.4 s retrieve latency.  One client per
# process is enough; ``httpx`` underneath it is safe for concurrent use.
#
# The cache is keyed on the identity of the resolved factory so that rebinding
# ``_create_client`` (tests, alternate transports) takes effect instead of being
# silently served a stale client.
_CLIENT_CACHE: tuple | None = None
_CLIENT_CACHE_LOCK = threading.Lock()


def _shared_client():
    global _CLIENT_CACHE
    factory = _create_client
    cached = _CLIENT_CACHE
    if cached is not None and cached[0] is factory:
        return cached[1]
    with _CLIENT_CACHE_LOCK:
        cached = _CLIENT_CACHE
        if cached is not None and cached[0] is factory:
            return cached[1]
        client = factory()
        _CLIENT_CACHE = (factory, client)
        return client


def reset_client_cache() -> None:
    """Drop the reused embedding client (credential rotation, transport swap)."""
    global _CLIENT_CACHE
    with _CLIENT_CACHE_LOCK:
        _CLIENT_CACHE = None


def prewarm_client() -> bool:
    """Build the embedding client before the first request needs it.

    ``import google.genai`` costs 3.5 s and ``genai.Client()`` a further 1.3 s on the
    operator machine -- both are paid inside the first embedding-needing call, which put
    4.8 s in front of the first search of every fresh process (a fresh ``search`` at 5.02 s
    against 0.76 s for the next one in the same server).  Neither step performs network I/O
    (auth is lazy), so a long-lived server can pay for them while it is still starting.

    Best effort by construction: a server that cannot build the client must still serve
    lexical search, so every failure is swallowed and reported as ``False``.  Returns
    ``True`` when a client is cached.  Callers run this off the request path; see
    :func:`start_prewarm_thread`.
    """
    if not os.environ.get("GEMINI_API_KEY"):
        return False
    if embedding_transport() != "sdk":
        # The REST transport never builds a client, so paying 4.8 s to warm one would be pure
        # cost -- the opposite of what this function is for.
        return False
    try:
        _shared_client()
        return True
    except Exception as exc:  # noqa: BLE001 - prewarming must never fail a server start
        log.warning("Embedding client prewarm failed (%s: %s).", type(exc).__name__, exc)
        return False


def start_prewarm_thread() -> threading.Thread | None:
    """Pre-warm the client on a daemon thread, unless disabled or unnecessary.

    ``VECTOR_LAKE_EMBEDDING_PREWARM=off`` (or ``0``) disables it -- a host that starts many
    short-lived processes should not pay a background import it may never use.

    Returns the thread, or ``None`` when nothing was started.
    """
    if str(os.environ.get("VECTOR_LAKE_EMBEDDING_PREWARM", "")).strip().lower() in {"off", "0", "false"}:
        return None
    if not os.environ.get("GEMINI_API_KEY"):
        return None
    if embedding_transport() != "sdk":
        # Nothing to warm: the REST transport never constructs a client, so a thread that only
        # calls :func:`prewarm_client` would return False without doing anything.
        return None
    thread = threading.Thread(
        target=prewarm_client, name="embedding-client-prewarm", daemon=True
    )
    thread.start()
    return thread


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
    budget_seconds: float | None = None,
    durable_reservation: bool = True,
) -> list[list[float]]:
    """Embed ``contents``, optionally inside a caller-supplied time budget.

    A quota error used to sleep a flat 60 s per retry, which on its own exceeds
    the MCP client's 60 s call ceiling: the server would keep working on a request
    whose caller had already given up, and the caller saw a timeout instead of the
    real cause.  With ``budget_seconds`` the deadline is checked before every
    sleep, so an unaffordable wait surfaces as :class:`EmbeddingBudgetExceeded`
    and the search degrades with ``vector_notes`` explaining why.
    """
    last_error: Exception | None = None
    deadline = None if budget_seconds is None else time.monotonic() + float(budget_seconds)
    transport = embedding_transport()
    # Built lazily: this import is the 3.5 s the REST transport exists to avoid.
    provider_contents = _provider_contents(contents) if transport == "sdk" else []
    for attempt in range(config.max_retries + 1):
        try:
            limiter.reserve(request_tokens, deadline=deadline, durable=durable_reservation)
            if transport == "rest":
                return _rest_embed_contents(contents, config)
            response = client.models.embed_content(model=config.model, contents=provider_contents)
            return _validated_response_values(response, len(contents), config.dimension)
        except EmbeddingBudgetExceeded:
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= config.max_retries:
                break
            message = str(exc)
            is_quota = "429" in message or "RESOURCE_EXHAUSTED" in message or "quota" in message.lower()
            delay = 60.0 if is_quota else min(60.0, 2.0 ** attempt)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if delay > remaining:
                    raise EmbeddingBudgetExceeded(
                        f"retry after {type(exc).__name__} needs {delay:.0f}s but only "
                        f"{max(0.0, remaining):.1f}s of the caller's budget remains: {message[:200]}"
                    ) from exc
            time.sleep(delay)
    assert last_error is not None
    raise last_error


def embed_texts(
    texts: list[str],
    budget_seconds: float | None = None,
    durable_reservation: bool = True,
) -> list[list[float]]:
    """Shared validated embedding entrypoint for small runtime requests.

    ``budget_seconds`` bounds the total wait (rate-limit window plus retries).
    Omit it for batch work such as backfill, where waiting is the correct answer.

    ``durable_reservation=False`` keeps the request off the write lock, for
    callers on a read path; see :meth:`MinuteRateLimiter.reserve`.
    """
    if not texts or not os.environ.get("GEMINI_API_KEY"):
        return []
    config = load_embedding_rate_config()
    normalized = [str(text)[:config.max_chars_per_item] for text in texts]
    tokens = sum(estimate_embedding_tokens(text) for text in normalized)
    return _request_embeddings(
        _shared_client() if embedding_transport() == "sdk" else None,
        normalized,
        tokens,
        config,
        MinuteRateLimiter(config),
        budget_seconds=budget_seconds,
        durable_reservation=durable_reservation,
    )


def embedding_backfill(
    index_data: dict[str, Any],
    dry_run: bool = True,
    limit: int | None = None,
    include_existing: bool = False,
    bodies: dict[str, str] | None = None,
    body_loader: Callable[[list[str]], dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Backfill missing vector embeddings under Gemini RPM/TPM limits.

    ``body_loader`` lets a recurring caller embed a bounded batch without reading the whole
    body corpus; see :func:`_candidate_items`.
    """
    config = load_embedding_rate_config()
    coverage_before = embedding_coverage(index_data)
    items = _candidate_items(
        index_data,
        include_existing=include_existing,
        limit=limit,
        config=config,
        bodies=bodies,
        body_loader=body_loader,
    )
    batches = _batch_items(items, config)
    estimated_tokens = sum(int(item["tokens"]) for item in items)
    plan = {
        "dry_run": dry_run,
        "model": config.model,
        "rpm": config.rpm,
        "tpm": config.tpm,
        "utilization": config.utilization,
        "effective_rpm": config.effective_rpm,
        "effective_tpm": config.effective_tpm,
        "candidates": len(items),
        "estimated_tokens": estimated_tokens,
        "estimated_requests": len(batches),
        "max_chars_per_item": config.max_chars_per_item,
        "max_tokens_per_item": config.max_tokens_per_item,
        "coverage_before": coverage_before,
        "embedded": 0,
        "failed_batches": 0,
    }
    if dry_run or not items:
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

    run_id = uuid.uuid4().hex
    plan["run_id"] = run_id
    db_store.start_embedding_run(run_id, config.model, len(items))
    last_error = ""
    consecutive_failures = 0
    try:
        # Only the SDK transport needs a client.  Building one unconditionally imported
        # ``google.genai`` (and raised ImportError on a host without it) even though
        # ``_request_embeddings`` would ignore the client and post over REST -- the cost the
        # REST transport exists to remove, on the one path that embeds in bulk.
        client = _shared_client() if embedding_transport() == "sdk" else None
        limiter = MinuteRateLimiter(config)
        for batch in batches:
            contents = [item["text"] for item in batch]
            batch_tokens = sum(int(item["tokens"]) for item in batch)
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
        lock.release()
