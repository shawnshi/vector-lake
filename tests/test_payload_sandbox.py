from pathlib import Path

import pytest

from vector_lake import mcp_server


def test_payload_reader_rejects_general_codex_file(monkeypatch, tmp_path):
    monkeypatch.delenv("VECTOR_LAKE_PAYLOAD_ROOT", raising=False)
    outside = Path.home() / ".codex" / "config.toml"

    with pytest.raises(ValueError, match="approved agent sandbox"):
        mcp_server._read_payload(str(outside))


def test_visualize_output_dir_gate_rejects_non_host_path(tmp_path):
    """The dashboard writer is gated, and the gate fails closed.

    This surface had no test coverage at all, which is how the ad-hoc root list
    in ``visualize_vector_lake`` stayed inconsistent with ``_read_payload``.
    """
    outside = tmp_path / "outside"
    outside.mkdir()

    result = mcp_server.visualize_vector_lake(str(outside))

    assert result.startswith("Error:")
    assert not (outside / "vector_lake_graph.html").exists()


def test_filesystem_root_override_is_rejected(monkeypatch):
    """A root of ``C:\\`` or ``/`` would make the sandbox vacuous.

    ``VECTOR_LAKE_PAYLOAD_ROOT`` is a security boundary, so a value that makes
    every path "inside" it is refused loudly instead of silently downgrading the
    gate to a no-op.
    """
    import pytest as _pytest

    from vector_lake import host_env

    filesystem_root = str(Path.home().anchor or "/")
    monkeypatch.setenv("VECTOR_LAKE_PAYLOAD_ROOT", filesystem_root)

    with _pytest.raises(host_env.PayloadRootError, match="vacuous"):
        host_env.payload_root_from_env()


def test_payload_root_override_extends_the_builtin_sandboxes(monkeypatch, tmp_path):
    """``VECTOR_LAKE_PAYLOAD_ROOT`` is additive, not a replacement.

    Setting the override used to discard every built-in root, so an unrelated
    operator override silently revoked the repository-local ``brain/`` sandbox.
    Both the override and the built-in root must stay usable at the same time.
    """
    import vector_lake

    repo_proxy = tmp_path / "repo"
    monkeypatch.setattr(vector_lake, "get_extension_root", lambda: repo_proxy)
    monkeypatch.setenv("VECTOR_LAKE_PAYLOAD_ROOT", str(tmp_path / "override"))

    # Built-in root: <extension_root>/brain/<project>/scratch/<file>
    builtin = repo_proxy / "brain" / "proj" / "scratch" / "payload.json"
    builtin.parent.mkdir(parents=True, exist_ok=True)
    builtin.write_text("builtin", encoding="utf-8")
    assert mcp_server._read_payload(str(builtin)) == "builtin"

    # Override root: named explicitly, so no scratch shape is required.
    override = tmp_path / "override"
    override.mkdir()
    overridden = override / "payload.json"
    overridden.write_text("override", encoding="utf-8")
    assert mcp_server._read_payload(str(overridden)) == "override"


def test_builtin_sandbox_still_requires_the_scratch_shape(monkeypatch, tmp_path):
    """Outside a configured root, ``brain/`` payloads must sit under ``scratch/``."""
    import vector_lake

    repo_proxy = tmp_path / "repo"
    monkeypatch.setattr(vector_lake, "get_extension_root", lambda: repo_proxy)
    monkeypatch.delenv("VECTOR_LAKE_PAYLOAD_ROOT", raising=False)

    loose = repo_proxy / "brain" / "proj" / "payload.json"
    loose.parent.mkdir(parents=True, exist_ok=True)
    loose.write_text("nope", encoding="utf-8")

    with pytest.raises(ValueError, match="approved agent sandbox"):
        mcp_server._read_payload(str(loose))


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
