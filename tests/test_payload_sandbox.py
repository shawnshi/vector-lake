from pathlib import Path

import pytest

from vector_lake import mcp_server


def test_payload_reader_rejects_general_codex_file(monkeypatch, tmp_path):
    monkeypatch.delenv("VECTOR_LAKE_PAYLOAD_ROOT", raising=False)
    outside = Path.home() / ".codex" / "config.toml"

    with pytest.raises(ValueError, match="approved agent sandbox"):
        mcp_server._read_payload(str(outside))


def test_codex_scratch_requires_an_explicit_host_adapter_root(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("VECTOR_LAKE_PAYLOAD_ROOT", raising=False)
    monkeypatch.setenv(
        "VECTOR_LAKE_SUBAGENT_BRAIN_ROOT",
        str(tmp_path / "runtime-brain"),
    )
    payload_root = tmp_path / ".codex" / "brain"
    payload = payload_root / "session" / "scratch" / "payload.txt"
    payload.parent.mkdir(parents=True)
    payload.write_text("safe", encoding="utf-8")

    with pytest.raises(ValueError, match="approved agent sandbox"):
        mcp_server._read_payload(str(payload))

    monkeypatch.setenv("VECTOR_LAKE_PAYLOAD_ROOT", str(payload_root))
    assert mcp_server._read_payload(str(payload)) == "safe"


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


def test_payload_reader_rejects_invalid_utf8(monkeypatch, tmp_path):
    monkeypatch.setenv("VECTOR_LAKE_PAYLOAD_ROOT", str(tmp_path))
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"\xff\xfe")

    with pytest.raises(ValueError, match="strict UTF-8"):
        mcp_server._read_payload(str(payload))


def test_graph_output_requires_explicit_host_sandbox_roots(
    monkeypatch,
    tmp_path,
):
    output_dir = tmp_path / "artifacts"
    monkeypatch.delenv("VECTOR_LAKE_AGENT_SANDBOX_ROOTS", raising=False)
    monkeypatch.setattr(
        mcp_server.tools,
        "visualize_vector_lake",
        lambda value: f"ok:{value}",
    )

    rejected = mcp_server.visualize_vector_lake(str(output_dir))
    assert "VECTOR_LAKE_AGENT_SANDBOX_ROOTS" in rejected

    monkeypatch.setenv("VECTOR_LAKE_AGENT_SANDBOX_ROOTS", str(tmp_path))
    assert mcp_server.visualize_vector_lake(str(output_dir)) == f"ok:{output_dir}"


def test_payload_reader_rejects_reparse_path(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = root / "payload.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable for this Windows token")
    monkeypatch.setenv("VECTOR_LAKE_PAYLOAD_ROOT", str(root))

    with pytest.raises(ValueError, match="reparse point"):
        mcp_server._read_payload(str(link))
