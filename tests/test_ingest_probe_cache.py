"""Probe cache correctness on recognized synthetic CLI packages, no providers."""
from __future__ import annotations

import json
from pathlib import Path
import time

import pytest

from vector_lake import ingest_probe_cache as cache
from vector_lake.ingest_cli import CliCapabilities
from vector_lake.wiki_utils import get_meta_dir


def _package(tmp_path: Path, backend="gemini"):
    binary = tmp_path / f"{backend}.cmd"
    binary.write_text("synthetic trusted launcher", encoding="utf-8")
    root = tmp_path / "node_modules" / cache.PACKAGE_NAMES[backend]
    root.mkdir(parents=True)
    entry = root / "entry.py"
    entry.write_text("# synthetic CLI entry\n", encoding="utf-8")
    manifest = root / "package.json"
    manifest.write_text(json.dumps({"name": cache.PACKAGE_NAMES[backend], "version": "test-1", "bin": {backend: "entry.py"}}), encoding="utf-8")
    return str(binary), entry, manifest


def _get(binary, backend, calls, policy="policy-v1", required=frozenset(), failing=False):
    def probe():
        calls.append(backend)
        if failing:
            raise RuntimeError("full probe failed")
        return CliCapabilities(binary, tuple(sorted(required)))

    return cache.cached_capabilities(backend, binary, policy, time.monotonic() + 30, probe, CliCapabilities, required)


def test_warm_cache_skips_probe_but_entry_manifest_policy_changes_invalidate(tmp_path):
    binary, entry, manifest = _package(tmp_path)
    calls = []
    assert _get(binary, "gemini", calls) == _get(binary, "gemini", calls)
    assert calls == ["gemini"]
    entry.write_text("# changed entry\n", encoding="utf-8")
    _get(binary, "gemini", calls)
    payload = json.loads(manifest.read_text(encoding="utf-8")); payload["version"] = "test-2"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    _get(binary, "gemini", calls)
    _get(binary, "gemini", calls, policy="policy-v2")
    assert len(calls) == 4


@pytest.mark.parametrize("corrupt", ["{broken", "null", "[]", "{}", "x" * 65537], ids=["broken", "null", "array", "empty", "oversized"])
def test_corrupt_cache_is_full_probe_not_no_data(tmp_path, corrupt):
    binary, _, _ = _package(tmp_path)
    path = get_meta_dir() / "runtime" / "cli_capabilities_gemini.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(corrupt, encoding="utf-8")
    calls = []
    _get(binary, "gemini", calls)
    assert calls == ["gemini"]
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_expiry_clock_reversal_and_feature_removal_force_full_probe(tmp_path):
    binary, _, _ = _package(tmp_path, "codex")
    calls = []
    required = frozenset({"shell_tool", "skip_host_skill_discovery"})
    _get(binary, "codex", calls, required=required)
    path = get_meta_dir() / "runtime" / "cli_capabilities_codex.json"
    for change in ({"checked_at": 0}, {"checked_at": time.time() + 100}, {"features": []}, {"version": True}):
        saved = json.loads(path.read_text(encoding="utf-8")); saved.update(change)
        path.write_text(json.dumps(saved), encoding="utf-8")
        _get(binary, "codex", calls, required=required)
    assert len(calls) == 5
    assert len(list(path.parent.glob("cli_capabilities_*.json"))) == 1
    assert path.stat().st_size < 65536
    assert "authentication" not in path.read_text(encoding="utf-8")


def test_failed_probe_never_creates_a_success_entry(tmp_path):
    binary, _, _ = _package(tmp_path)
    calls = []
    with pytest.raises(RuntimeError, match="full probe failed"):
        _get(binary, "gemini", calls, failing=True)
    assert not (get_meta_dir() / "runtime" / "cli_capabilities_gemini.json").exists()


def test_unknown_launcher_is_uncached_and_expired_deadline_does_not_probe(tmp_path):
    binary = tmp_path / "unknown.cmd"
    binary.write_text("opaque command", encoding="utf-8")
    calls = []
    _get(str(binary), "gemini", calls)
    _get(str(binary), "gemini", calls)
    assert calls == ["gemini", "gemini"]
    recognized, _, _ = _package(tmp_path)
    with pytest.raises(RuntimeError, match="deadline exhausted"):
        cache.cached_capabilities("gemini", recognized, "policy", 0,
                                  lambda: pytest.fail("expired deadline probed"), CliCapabilities)


def test_identity_change_during_probe_is_not_cached(tmp_path):
    binary, entry, _ = _package(tmp_path)

    def probe():
        entry.write_text("# changed during validation\n", encoding="utf-8")
        return CliCapabilities(binary)

    with pytest.raises(RuntimeError, match="identity changed"):
        cache.cached_capabilities("gemini", binary, "policy", time.monotonic() + 30, probe, CliCapabilities)
    assert not (get_meta_dir() / "runtime" / "cli_capabilities_gemini.json").exists()
