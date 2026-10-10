"""VL-A05: caller deadlines cover preparation and provider attempts."""
from types import SimpleNamespace
import http.server
import json
import threading
import time
import urllib.request

import pytest

from vector_lake import embedding_scheduler as scheduler


class Limiter:
    def reserve(self, *_args, **_kwargs):
        pass


def _configure(monkeypatch, transport="sdk"):
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_TRANSPORT", transport)
    config = scheduler.EmbeddingRateConfig(dimension=2, max_retries=2)
    monkeypatch.setattr(scheduler, "load_embedding_rate_config", lambda: config)
    monkeypatch.setattr(scheduler, "_provider_contents", lambda texts: list(texts))
    monkeypatch.setattr(scheduler, "MinuteRateLimiter", lambda _config: Limiter())
    return config


def test_client_preparation_cannot_restart_the_caller_budget(monkeypatch):
    _configure(monkeypatch)
    clock, requests = [0.0], []
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: clock[0])

    def embed(**kwargs):
        requests.append(kwargs)
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0, 2.0])])

    def client():
        clock[0] = 2.0
        return SimpleNamespace(models=SimpleNamespace(embed_content=embed))

    monkeypatch.setattr(scheduler, "_shared_client", client)
    with pytest.raises(scheduler.EmbeddingBudgetExceeded):
        scheduler.embed_texts(["synthetic"], budget_seconds=0.5)
    assert requests == []


def test_sdk_timeout_uses_remaining_budget_and_disables_sdk_retries(monkeypatch):
    config = _configure(monkeypatch)
    clock, calls = [0.0], []
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: clock[0])

    class DelayedLimiter:
        def reserve(self, *_args, **_kwargs):
            clock[0] = 0.3

    def embed(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0, 2.0])])

    client = SimpleNamespace(models=SimpleNamespace(embed_content=embed))
    assert scheduler._request_embeddings(client, ["synthetic"], 1, config, DelayedLimiter(), budget_seconds=0.5) == [[1.0, 2.0]]
    options = calls[0]["config"]["http_options"]
    assert 1 <= options["timeout"] <= 200
    assert options["retry_options"]["attempts"] == 1
    # Validate against the installed SDK's real request schema, without networking.
    from google.genai import types
    assert types.EmbedContentConfig.model_validate(calls[0]["config"]).http_options.timeout == options["timeout"]


@pytest.mark.parametrize("transport", ["sdk", "rest"])
def test_expired_provider_result_is_not_accepted(monkeypatch, transport):
    config = _configure(monkeypatch, transport)
    clock = [0.0]
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: clock[0])

    def late_values(*_args, **_kwargs):
        clock[0] = 1.0
        return [[1.0, 2.0]]

    if transport == "rest":
        monkeypatch.setattr(scheduler, "_rest_embed_contents", late_values)
        client = None
    else:
        def late_sdk(**kwargs):
            return SimpleNamespace(embeddings=[SimpleNamespace(values=v) for v in late_values()])
        client = SimpleNamespace(models=SimpleNamespace(embed_content=late_sdk))
    with pytest.raises(scheduler.EmbeddingBudgetExceeded):
        scheduler._request_embeddings(client, ["synthetic"], 1, config, Limiter(), budget_seconds=0.2)


@pytest.mark.parametrize("transport", ["sdk", "rest"])
def test_actual_delayed_headers_obey_remaining_timeout(monkeypatch, record_property, transport):
    config = _configure(monkeypatch, transport)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            time.sleep(0.4)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"embeddings": [{"values": [1.0, 2.0]}]}).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass  # Expected when the deadline closes this synthetic connection.

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    original = urllib.request.urlopen

    def loopback(request, timeout):
        local = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/", data=request.data, headers=dict(request.header_items()), method="POST")
        return original(local, timeout=timeout)

    client = None
    if transport == "rest":
        monkeypatch.setattr(urllib.request, "urlopen", loopback)
    else:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key="synthetic-test-key", http_options=types.HttpOptions(
            base_url=f"http://127.0.0.1:{server.server_port}",
            retry_options=types.HttpRetryOptions(attempts=1),
        ))
    started = time.perf_counter()
    try:
        with pytest.raises(scheduler.EmbeddingBudgetExceeded):
            scheduler._request_embeddings(client, ["synthetic"], 1, config, Limiter(), budget_seconds=0.15)
        elapsed = time.perf_counter() - started
        record_property("deadline_elapsed_ms", round(elapsed * 1000, 3))
        assert elapsed < 0.6
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
    assert not worker.is_alive()
    if client is not None:
        client.close()


def test_preparation_expiry_does_not_acquire_a_client(monkeypatch):
    _configure(monkeypatch)
    clock = [0.0]
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: clock[0])
    def slow_tokens(_text):
        clock[0] = 1.0
        return 1
    monkeypatch.setattr(scheduler, "estimate_embedding_tokens", slow_tokens)
    monkeypatch.setattr(scheduler, "_shared_client", lambda: pytest.fail("expired work acquired a client"))
    with pytest.raises(scheduler.EmbeddingBudgetExceeded):
        scheduler.embed_texts(["synthetic"], budget_seconds=0.2)


@pytest.mark.parametrize("budget", [float("nan"), float("inf")])
def test_nonfinite_budget_rejected_before_client_acquisition(monkeypatch, budget):
    _configure(monkeypatch)
    monkeypatch.setattr(scheduler, "_shared_client", lambda: pytest.fail("invalid budget acquired a client"))
    with pytest.raises(ValueError, match="finite"):
        scheduler.embed_texts(["synthetic"], budget_seconds=budget)


def test_subsecond_configured_transport_timeout_is_preserved(monkeypatch):
    monkeypatch.setenv("VECTOR_LAKE_EMBEDDING_TIMEOUT_MS", "150")
    assert scheduler._transport_timeout(None) == 0.15
