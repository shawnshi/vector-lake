from pathlib import Path

import pytest

from vector_lake import mcp_server


def test_payload_reader_rejects_general_codex_file(monkeypatch, tmp_path):
    monkeypatch.delenv("VECTOR_LAKE_PAYLOAD_ROOT", raising=False)
    outside = Path.home() / ".codex" / "config.toml"

    with pytest.raises(ValueError, match="approved agent sandbox"):
        mcp_server._read_payload(str(outside))


def test_payload_reader_enforces_size_limit(monkeypatch, tmp_path):
    monkeypatch.setenv("VECTOR_LAKE_PAYLOAD_ROOT", str(tmp_path))
    monkeypatch.setenv("VECTOR_LAKE_PAYLOAD_MAX_BYTES", "4")
    payload = tmp_path / "payload.txt"
    payload.write_text("12345", encoding="utf-8")

    with pytest.raises(ValueError, match="exceeds 4 bytes"):
        mcp_server._read_payload(str(payload))


def test_payload_reader_accepts_configured_root(monkeypatch, tmp_path):
    monkeypatch.setenv("VECTOR_LAKE_PAYLOAD_ROOT", str(tmp_path))
    payload = tmp_path / "payload.txt"
    payload.write_text("safe", encoding="utf-8")

    assert mcp_server._read_payload(str(payload)) == "safe"
