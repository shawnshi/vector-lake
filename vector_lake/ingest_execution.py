"""Owned runner circuit state and bounded, non-content phase measurements."""
from __future__ import annotations

import hashlib
import json
import math
import os
from collections import deque
from pathlib import Path
from statistics import median
import threading
import time

FAILURE_LIMIT = 3
METRIC_WINDOW = 128
PHASES = ("queue_wait", "claim", "model", "finalize", "cycle")
FAILURE_CODES = frozenset({"process_exit", "timeout", "launch_error", "output_invalid", "delivery_error", "host_runtime_error"})


class BackendFailure(str):
    """String-compatible diagnostic; the host keeps a separate safe failure code."""

    def __new__(cls, message: str, code: str):
        result = super().__new__(cls, message)
        result.code = code if code in FAILURE_CODES | {"source_changed", "paused"} else "delivery_error"
        return result


class BackendCircuit:
    """One root consumer owns this file; worker threads serialize through this lock."""

    def __init__(self, path: Path, backend: str, command: str, *, reset: bool = False, clock=time.time):
        self.path = path
        self.clock = clock
        self.backend = backend
        self.key = hashlib.sha256(f"{backend}\0{command}".encode()).hexdigest()
        self.lock = threading.RLock()
        self.samples = {phase: deque(maxlen=METRIC_WINDOW) for phase in PHASES}
        self.counts = dict.fromkeys(PHASES, 0)
        self.invalid_samples = 0
        self.state = self._closed()
        if reset:
            self._save()
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._save()
            return
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Backend circuit state is unreadable; explicit owner reset is required") from exc
        if not self._valid(payload):
            raise RuntimeError("Backend circuit state is invalid; explicit owner reset is required")
        if payload["backend_key"] == self.key:
            self.state = payload

    def _closed(self):
        return {"version": 1, "backend_key": self.key, "status": "closed", "failures": 0,
                "retry_at": 0.0, "last_code": ""}

    @staticmethod
    def _valid(payload):
        if not isinstance(payload, dict) or set(payload) != {"version", "backend_key", "status", "failures", "retry_at", "last_code"}:
            return False
        count, retry = payload["failures"], payload["retry_at"]
        if (isinstance(payload["version"], bool) or payload["version"] != 1 or not isinstance(payload["backend_key"], str)
                or len(payload["backend_key"]) != 64 or any(character not in "0123456789abcdef" for character in payload["backend_key"])
                or isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= FAILURE_LIMIT
                or isinstance(retry, bool) or not isinstance(retry, (int, float)) or not math.isfinite(retry) or retry < 0
                or not isinstance(payload["last_code"], str) or payload["last_code"] not in FAILURE_CODES | {""}):
            return False
        return ((payload["status"] == "closed" and count == 0 and retry == 0 and payload["last_code"] == "")
                or (payload["status"] == "cooldown" and 0 < count < FAILURE_LIMIT and payload["last_code"] in FAILURE_CODES)
                or (payload["status"] == "open" and count == FAILURE_LIMIT and retry == 0 and payload["last_code"] in FAILURE_CODES))

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.state, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)

    def ready(self) -> bool:
        with self.lock:
            return self.state["status"] != "open" and self.clock() >= self.state["retry_at"]

    def failure(self, code: str):
        with self.lock:
            count = min(FAILURE_LIMIT, self.state["failures"] + 1)
            self.state.update(failures=count, last_code=code if code in FAILURE_CODES else "delivery_error",
                              status="open" if count == FAILURE_LIMIT else "cooldown",
                              retry_at=0.0 if count == FAILURE_LIMIT else self.clock() + 5 * (2 ** (count - 1)))
            self._save()

    def success(self):
        with self.lock:
            # An already-in-flight success cannot silently reopen an owner-paused backend.
            if self.state["status"] == "open" or self.state["status"] == "closed":
                return
            self.state = self._closed()
            self._save()

    def snapshot(self):
        with self.lock:
            return {"backend": self.backend, "status": self.state["status"], "failures": self.state["failures"],
                    "failure_limit": FAILURE_LIMIT, "retry_at": self.state["retry_at"], "last_code": self.state["last_code"]}

    def delay(self, idle_seconds: float) -> float:
        with self.lock:
            if self.state["status"] == "open":
                return max(5.0, idle_seconds)
            return max(1.0, min(max(5.0, idle_seconds), self.state["retry_at"] - self.clock()))

    def observe(self, phase: str, milliseconds: float):
        with self.lock:
            if phase not in self.samples or not math.isfinite(milliseconds) or milliseconds < 0:
                self.invalid_samples += 1
                return
            self.samples[phase].append(milliseconds)
            self.counts[phase] += 1

    def timings(self):
        with self.lock:
            phases = {}
            for phase, values in self.samples.items():
                ordered = sorted(values)
                phases[phase] = {"count": self.counts[phase], "window_count": len(ordered),
                                 "p50_ms": median(ordered) if ordered else None,
                                 "p95_ms": ordered[max(0, math.ceil(len(ordered) * .95) - 1)] if ordered else None}
            return {"unit": "ms", "window_limit": METRIC_WINDOW, "invalid_samples": self.invalid_samples, "phases": phases}
