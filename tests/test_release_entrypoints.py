import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]

ROOT_THIN_ENTRYPOINTS = frozenset(
    {
        "cli.py",
        "watchdog_sync.py",
    }
)
PACKAGE_MODULE_ENTRYPOINTS = frozenset(
    {
        "vector_lake/indexer.py",
        "vector_lake/ingest_worker.py",
        "vector_lake/mcp_server.py",
        "vector_lake/topology_worker.py",
        "vector_lake/watchdog_app.py",
    }
)
SUPPORTED_SCRIPT_ENTRYPOINTS = frozenset(
    {
        "scripts/benchmark_multi_host_runtime.py",
        "scripts/compile_domain_overviews.py",
        "scripts/semantic_merge.py",
        "scripts/validate_purpose_contract.py",
        "scripts/vector_lake_mcp.py",
    }
)
LEGACY_FAIL_CLOSED_ENTRYPOINTS = frozenset(
    {
        "scripts/community_clustering_daemon.py",
        "scripts/semantic_dedup_daemon.py",
    }
)
RELEASE_ENTRYPOINTS = (
    ROOT_THIN_ENTRYPOINTS
    | PACKAGE_MODULE_ENTRYPOINTS
    | SUPPORTED_SCRIPT_ENTRYPOINTS
    | LEGACY_FAIL_CLOSED_ENTRYPOINTS
)
REMOVED_UNSUPPORTED_ENTRYPOINTS = (
    "reset_jobs.py",
    "check_jobs.py",
    "scripts/launch_janitor_swarm.py",
)

PURPOSE_CONTRACT = """---
purpose_version: "12.0"
intent_keywords: [healthcare]
intent_weight_boost: 0.1
scope:
  core: [healthcare]
  edge: [technology]
  excluded: [irrelevant]
  marketing_noise: [unsupported]
evidence_tiers:
  engineering-performance: Reproducible engineering evidence
sir_registry:
  - id: SIR-1
    status: active
    review_after: 2026-01-01
    signal_keywords: [one]
  - id: SIR-2
    status: active
    review_after: 2026-01-01
    signal_keywords: [two]
  - id: SIR-3
    status: active
    review_after: 2026-01-01
    signal_keywords: [three]
  - id: SIR-4
    status: active
    review_after: 2026-01-01
    signal_keywords: [four]
  - id: SIR-5
    status: active
    review_after: 2026-01-01
    signal_keywords: [five]
synthesis_policy:
  min_distinct_sources: 2
  min_tension_intensity: 0.5
---
Release entrypoint validation fixture.
"""


def _is_main_guard(statement: ast.stmt) -> bool:
    if not isinstance(statement, ast.If):
        return False
    test = statement.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def _has_main_guard(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(_is_main_guard(statement) for statement in tree.body)


def _discover_release_entrypoints() -> set[str]:
    candidates = (
        *ROOT.glob("*.py"),
        *(ROOT / "scripts").glob("*.py"),
        *(ROOT / "vector_lake").glob("*.py"),
    )
    return {
        path.relative_to(ROOT).as_posix()
        for path in candidates
        if _has_main_guard(path)
    }


def _isolated_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    memory_root = tmp_path / "memory"
    meta_root = memory_root / "wiki" / ".meta"
    env = os.environ.copy()
    env.pop("VECTOR_LAKE_ENABLE_LEGACY_UNSAFE_DAEMONS", None)
    env.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(ROOT),
            "VECTOR_LAKE_DB_PATH": str(meta_root / "vector_lake.db"),
            "VECTOR_LAKE_MCP_SURFACE": "full",
            "VECTOR_LAKE_MEMORY_DIR": str(memory_root),
            "VECTOR_LAKE_META_DIR": str(meta_root),
        }
    )
    return env, memory_root, meta_root


def _run_script(
    relative_path: str,
    *arguments: str,
    tmp_path: Path,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env, _, _ = _isolated_env(tmp_path)
    return subprocess.run(
        [sys.executable, "-B", str(ROOT / relative_path), *arguments],
        cwd=tmp_path,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _result_detail(result: subprocess.CompletedProcess[str]) -> str:
    return (
        f"returncode={result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def _fresh_shell_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "VECTOR_LAKE_MEMORY_DIR",
        "VECTOR_LAKE_META_DIR",
        "VECTOR_LAKE_DB_PATH",
    ):
        env.pop(name, None)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(ROOT),
            "VECTOR_LAKE_MCP_SURFACE": "full",
        }
    )
    return env


def test_release_entrypoint_inventory_is_exact():
    assert _discover_release_entrypoints() == RELEASE_ENTRYPOINTS


def test_storage_entrypoints_bind_packaged_runtime_authority_before_work():
    authority_bound = {
        "watchdog_sync.py",
        "vector_lake/indexer.py",
        "vector_lake/ingest_worker.py",
        "vector_lake/mcp_server.py",
        "vector_lake/watchdog_app.py",
        "scripts/compile_domain_overviews.py",
        "scripts/semantic_merge.py",
        "scripts/validate_purpose_contract.py",
        "scripts/vector_lake_mcp.py",
    }
    for relative_path in authority_bound:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "bootstrap_runtime_paths" in source, relative_path


@pytest.mark.parametrize("relative_path", sorted(RELEASE_ENTRYPOINTS))
def test_every_release_entrypoint_compiles(relative_path):
    path = ROOT / relative_path
    source = path.read_bytes()

    compile(source, str(path), "exec", dont_inherit=True)


@pytest.mark.parametrize(
    "relative_path",
    REMOVED_UNSUPPORTED_ENTRYPOINTS,
)
def test_removed_unsupported_entrypoints_are_not_packaged(relative_path):
    assert relative_path not in RELEASE_ENTRYPOINTS
    assert not (ROOT / relative_path).exists()


def test_capacity_benchmark_help_runs_from_isolated_cwd(tmp_path):
    result = _run_script(
        "scripts/benchmark_multi_host_runtime.py",
        "--help",
        tmp_path=tmp_path,
    )

    assert result.returncode == 0, _result_detail(result)
    assert "--max-total-rss-mib" in result.stdout
    assert not list(tmp_path.glob("vector-lake-capacity-*"))


def test_cli_help_runs_against_an_isolated_runtime(tmp_path):
    result = _run_script("cli.py", "--help", tmp_path=tmp_path)

    assert result.returncode == 0, _result_detail(result)
    assert "Vector Lake CLI Gateway" in result.stdout
    assert "usage:" in result.stdout


def test_cli_fresh_shell_ignores_stale_dotenv_storage_paths(tmp_path):
    home = tmp_path / "fresh-home"
    dotenv_root = home / ".gemini"
    dotenv_root.mkdir(parents=True)
    stale_root = tmp_path / "stale-gemini-memory"
    stale_meta = stale_root / "wiki" / ".meta"
    stale_db = stale_meta / "stale.db"
    (dotenv_root / ".env").write_text(
        "\n".join(
            (
                f"VECTOR_LAKE_MEMORY_DIR={stale_root}",
                f"VECTOR_LAKE_META_DIR={stale_meta}",
                f"VECTOR_LAKE_DB_PATH={stale_db}",
            )
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-B", str(ROOT / "cli.py"), "schema-migrate"],
        cwd=tmp_path,
        env=_fresh_shell_env(home),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, _result_detail(result)
    report = json.loads(result.stdout)
    expected_db = home / "MEMORY" / "wiki" / ".meta" / "vector_lake.db"
    assert Path(report["database_path"]).resolve() == expected_db.resolve()
    assert not stale_root.exists()


@pytest.mark.parametrize(
    "adapter",
    ("codex", "agent-plugin", "gemini"),
)
def test_host_adapter_launches_shared_stdio_contract_without_pythonpath(
    adapter,
    tmp_path,
):
    if adapter == "codex":
        payload = json.loads(
            (ROOT / ".codex-plugin" / "mcp.json").read_text(encoding="utf-8")
        )
    elif adapter == "agent-plugin":
        payload = json.loads((ROOT / "mcp.json").read_text(encoding="utf-8"))
    else:
        payload = json.loads(
            (ROOT / "gemini-extension.json").read_text(encoding="utf-8")
        )
    server = payload["mcpServers"]["vector-lake-mcp"]
    plugin_data = tmp_path / "plugin-data"
    plugin_data.mkdir()
    replacements = {
        "${PLUGIN_ROOT}": str(ROOT),
        "${PLUGIN_DATA}": str(plugin_data),
        "${extensionPath}": str(ROOT),
    }

    def materialize(value):
        for marker, replacement in replacements.items():
            value = value.replace(marker, replacement)
        return value

    env, _, _ = _isolated_env(tmp_path)
    env.pop("PYTHONPATH", None)
    env.update(
        {key: materialize(value) for key, value in server.get("env", {}).items()}
    )
    cwd = ROOT if server.get("cwd") == "." else Path(materialize(server["cwd"]))
    args = [materialize(value) for value in server["args"]]

    result = subprocess.run(
        [sys.executable, "-B", *args],
        cwd=cwd,
        env=env,
        input="",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert server["command"] == "python"
    assert result.returncode == 0, _result_detail(result)


def test_launcher_stdio_initializes_lists_tools_and_runs_quick_doctor(tmp_path):
    env, _, _ = _isolated_env(tmp_path)
    env.pop("PYTHONPATH", None)
    launcher = ROOT / "scripts" / "vector_lake_mcp.py"
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            str(launcher),
            "--profile",
            "default",
            "--surface",
            "full",
        ],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    def request(payload: dict, *, expect_response: bool = True) -> dict:
        assert process.stdin is not None
        process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        process.stdin.flush()
        if not expect_response:
            return {}
        assert process.stdout is not None
        response = json.loads(process.stdout.readline())
        assert response.get("error") is None
        return response["result"]

    try:
        initialized = request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "release-smoke", "version": "1"},
                },
            }
        )
        request(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
            expect_response=False,
        )
        listed = request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }
        )
        called = request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "doctor_vector_lake",
                    "arguments": {"mode": "quick"},
                },
            }
        )

        assert initialized["protocolVersion"] == "2024-11-05"
        assert len(listed["tools"]) == 65
        doctor = json.loads(called["content"][0]["text"])
        assert doctor["mode"] == "quick"
        assert doctor["semantic_readiness"] == {
            "status": "not_checked",
            "reason": "requires_deep_doctor",
        }
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            returncode = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            returncode = process.wait(timeout=5)
        assert returncode == 0


def test_cli_dotenv_loading_requires_an_explicit_absolute_file(
    tmp_path,
    monkeypatch,
):
    from vector_lake import cli_app

    calls = []

    class DotenvProbe:
        @staticmethod
        def load_dotenv(path):
            calls.append(Path(path))

    monkeypatch.setattr(cli_app, "dotenv", DotenvProbe())
    monkeypatch.delenv("VECTOR_LAKE_ENV_FILE", raising=False)
    cli_app._load_env()
    assert calls == []

    monkeypatch.setenv("VECTOR_LAKE_ENV_FILE", "relative.env")
    with pytest.raises(RuntimeError, match="absolute path"):
        cli_app._load_env()

    env_file = tmp_path / "runtime.env"
    env_file.write_text("VECTOR_LAKE_QUERY_EMBEDDING=0\n", encoding="utf-8")
    monkeypatch.setenv("VECTOR_LAKE_ENV_FILE", str(env_file.resolve()))
    cli_app._load_env()
    assert calls == [env_file.resolve()]


def test_cli_partial_runtime_override_fails_before_storage_write(tmp_path):
    home = tmp_path / "partial-home"
    explicit_memory = tmp_path / "partial-memory"
    env = _fresh_shell_env(home)
    env["VECTOR_LAKE_MEMORY_DIR"] = str(explicit_memory)

    result = subprocess.run(
        [sys.executable, "-B", str(ROOT / "cli.py"), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 1, _result_detail(result)
    assert "runtime path overrides must be set together" in result.stderr
    assert not explicit_memory.exists()


def test_watchdog_stop_runs_against_an_isolated_runtime(tmp_path):
    _, _, meta_root = _isolated_env(tmp_path)
    result = _run_script("watchdog_sync.py", "--stop", tmp_path=tmp_path)

    assert result.returncode == 0, _result_detail(result)
    assert "Watchdog stop requested:" in result.stdout
    assert (meta_root / ".watchdog.stop").is_file()


def test_packaged_mcp_manifest_entrypoint_starts_and_closes_on_eof(tmp_path):
    manifest = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    server = manifest["mcpServers"]["vector-lake-mcp"]
    env, _, _ = _isolated_env(tmp_path)

    assert server["command"] == "python"
    assert server["args"] == [
        "scripts/vector_lake_mcp.py",
        "--profile",
        "default",
        "--surface",
        "full",
    ]
    assert server["cwd"] == "."
    assert "PYTHONPATH" not in server.get("env", {})

    result = subprocess.run(
        [sys.executable, "-B", *server["args"]],
        cwd=ROOT,
        env=env,
        input="",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, _result_detail(result)


def test_mcp_launcher_is_independent_of_cwd_and_pythonpath(tmp_path):
    env, _, _ = _isolated_env(tmp_path)
    env.pop("PYTHONPATH", None)
    launcher = ROOT / "scripts" / "vector_lake_mcp.py"

    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(launcher),
            "--profile",
            "default",
            "--surface",
            "readonly",
        ],
        cwd=tmp_path,
        env=env,
        input="",
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, _result_detail(result)


@pytest.mark.parametrize(
    ("relative_path", "expected_returncode", "expected_output"),
    [
        ("scripts/compile_domain_overviews.py", 0, "Index not found"),
        ("scripts/semantic_merge.py", 1, "Usage:"),
    ],
)
def test_supported_operator_scripts_have_isolated_smoke_paths(
    tmp_path,
    relative_path,
    expected_returncode,
    expected_output,
):
    result = _run_script(relative_path, tmp_path=tmp_path)

    assert result.returncode == expected_returncode, _result_detail(result)
    assert expected_output in result.stdout + result.stderr


def test_purpose_contract_validator_runs_against_an_isolated_fixture(tmp_path):
    _, memory_root, _ = _isolated_env(tmp_path)
    memory_root.mkdir(parents=True, exist_ok=True)
    (memory_root / "purpose.md").write_text(PURPOSE_CONTRACT, encoding="utf-8")

    result = _run_script(
        "scripts/validate_purpose_contract.py",
        tmp_path=tmp_path,
    )

    assert result.returncode == 0, _result_detail(result)
    assert result.stdout.strip() == "purpose-contract validation: OK"


@pytest.mark.parametrize(
    "relative_path",
    sorted(LEGACY_FAIL_CLOSED_ENTRYPOINTS),
)
def test_legacy_operator_entrypoints_fail_closed_without_touching_storage(
    tmp_path,
    relative_path,
):
    before = set(tmp_path.rglob("*"))

    result = _run_script(relative_path, tmp_path=tmp_path)

    assert result.returncode == 78, _result_detail(result)
    assert "DEPRECATED/UNSUPPORTED" in result.stderr
    assert "disabled by default" in result.stderr
    assert set(tmp_path.rglob("*")) == before
