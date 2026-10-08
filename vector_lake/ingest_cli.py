"""Tool-free Gemini/Codex execution behind the existing ingest JSON seam."""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from vector_lake.ingest_model_contract import build_cli_prompt, extract_result, result_schema
from vector_lake.process_control import model_timeout_seconds, run_contained
from vector_lake.template_loader import read_template

CODEX_REQUIRED_FEATURES = frozenset({
    "shell_tool", "unified_exec", "apps", "hooks", "plugins", "multi_agent", "view_image",
    "browser_use", "computer_use", "image_generation", "skip_host_skill_discovery",
})
CODEX_DISABLED_FEATURES = CODEX_REQUIRED_FEATURES - {"skip_host_skill_discovery"} | frozenset({
    "code_mode", "code_mode_host", "code_mode_only", "code_mode_prewarm", "agent_message_board",
    "memories", "external_agent_memory_import", "skill_search", "skill_mcp_dependency_install",
    "workspace_dependencies", "artifact", "remote_plugin", "plugin_sharing", "js_repl", "psp",
    "request_permissions_tool", "guardian_conversation_history_tools", "goals", "sleep_tool",
    "in_app_browser", "in_app_local_automation", "standalone_web_search",
})


@dataclass(frozen=True)
class CliCapabilities:
    binary: str
    features: tuple[str, ...] = ()


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("model execution deadline exhausted")
    return remaining


def _launcher(argv: list[str]) -> list[str]:
    if os.name == "nt" and argv[0].lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", *argv]
    return argv


def find_cli_binary(backend: str) -> str | None:
    # Defer the resolver so capability probing creates no load-order dependency on the registry.
    from vector_lake.ingest_backend import find_cli_binary as resolve_binary
    return resolve_binary(backend)


def check_cli(backend: str, *, deadline: float | None = None) -> CliCapabilities:
    if backend not in {"codex", "gemini"}:
        raise ValueError("CLI ingest backend must be codex or gemini")
    if deadline is None:
        deadline = time.monotonic() + 20
    binary = find_cli_binary(backend)
    if not binary:
        raise RuntimeError(f"{backend} ingest backend: CLI executable is unavailable")
    probe = [binary, "exec", "--help"] if backend == "codex" else [binary, "--help"]
    proc = run_contained(_launcher(probe), capture_output=True, text=True, encoding="utf-8",
                         timeout=min(10, _remaining(deadline)), shell=False)
    flags = ("--sandbox", "--ephemeral", "--output-schema", "--output-last-message") if backend == "codex" else (
        "--output-format", "--extensions", "--policy", "--prompt",
    )
    if proc.returncode or any(flag not in proc.stdout for flag in flags):
        raise RuntimeError(f"{backend} CLI lacks required headless/safety capabilities (help exit {proc.returncode})")
    if backend == "gemini":
        return CliCapabilities(binary)
    proc = run_contained(_launcher([binary, "features", "list"]), capture_output=True, text=True,
                         encoding="utf-8", timeout=min(10, _remaining(deadline)), shell=False)
    features = tuple(line.split()[0] for line in proc.stdout.splitlines()
                     if len(line.split()) >= 3 and line.split()[-1] in {"true", "false"})
    if proc.returncode or not CODEX_REQUIRED_FEATURES.issubset(features):
        raise RuntimeError("codex CLI does not expose the required tool-disable feature gates")
    return CliCapabilities(binary, features)


def _codex_argv(capabilities: CliCapabilities, directory: Path) -> list[str]:
    schema = directory / "result-schema.json"
    schema.write_text(json.dumps(result_schema()), encoding="utf-8")
    # CLI overrides replace these tables; do not import the user's MCPs, plugins or hooks.
    overrides = ["mcp_servers={}", "apps={}", "plugins={}", "notify=[]",
                 'web_search="disabled"', "project_doc_max_bytes=0"]
    # Disable execution/context tools, not authentication, secret storage or sandbox protections.
    overrides += [f"features.{name}=false" for name in capabilities.features
                  if name in CODEX_DISABLED_FEATURES]
    overrides.append("features.skip_host_skill_discovery=true")
    argv = [capabilities.binary, "-a", "never"]
    for value in overrides:
        argv += ["-c", value]
    return argv + ["exec", "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check",
                   "--output-schema", str(schema), "--output-last-message", str(directory / "answer.json"), "-"]


def _gemini_argv(capabilities: CliCapabilities, directory: Path, env: dict) -> list[str]:
    policy = directory / "deny-tools.toml"
    policy.write_text('[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 999\n', encoding="utf-8")
    settings = directory / "system-settings.json"
    settings.write_text(json.dumps({
        "admin": {"mcp": {"enabled": False}, "extensions": {"enabled": False}},
        "tools": {"core": ["__vector_lake_no_tools__"], "allowed": [], "exclude": ["*"]},
        "hooksConfig": {"enabled": False}, "hooks": {},
        "context": {"fileName": ["__vector_lake_no_context__"], "includeDirectories": [],
                    "includeDirectoryTree": False, "loadMemoryFromIncludeDirectories": False},
        "policyPaths": [str(policy)],
    }), encoding="utf-8")
    # System settings outrank user/project settings without moving or copying credentials.
    env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] = str(settings)
    return [capabilities.binary, "--extensions", "none", "--policy", str(policy),
            "--output-format", "json", "--prompt", read_template("prompts/ingest/cli_bootstrap.md")]


def _execution_capabilities(backend: str, deadline: float) -> CliCapabilities:
    """Cache only recognized host CLI identities; explicit --check still probes fresh."""
    from vector_lake.ingest_probe_cache import cached_capabilities

    _remaining(deadline)
    binary = find_cli_binary(backend)
    if not binary:
        return check_cli(backend, deadline=deadline)
    try:
        policy = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except OSError:
        return check_cli(backend, deadline=deadline)
    return cached_capabilities(backend, binary, policy, deadline,
                               lambda: check_cli(backend, deadline=deadline), CliCapabilities,
                               CODEX_REQUIRED_FEATURES if backend == "codex" else frozenset())


def invoke_cli(packet: dict, backend: str, *, use_cache: bool = True) -> dict:
    try:
        deadline = float(os.environ.get("VECTOR_LAKE_MODEL_DEADLINE_MONOTONIC",
                                        time.monotonic() + model_timeout_seconds()))
    except ValueError as exc:
        raise ValueError("model deadline must be a finite monotonic timestamp") from exc
    if not math.isfinite(deadline):
        raise ValueError("model deadline must be a finite monotonic timestamp")
    _remaining(deadline)
    capabilities = _execution_capabilities(backend, deadline) if use_cache else check_cli(backend, deadline=deadline)
    prompt = build_cli_prompt(packet)
    env = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix=f"vector-lake-{backend}-") as temporary:
        directory = Path(temporary)
        argv = _codex_argv(capabilities, directory) if backend == "codex" else _gemini_argv(capabilities, directory, env)
        proc = run_contained(_launcher(argv), input=prompt, capture_output=True, text=True,
                             encoding="utf-8", timeout=_remaining(deadline), shell=False,
                             cwd=str(directory), env=env)
        if proc.returncode:
            # Do not persist model output/source text or echo possible authentication secrets.
            raise RuntimeError(f"{backend} model process exited {proc.returncode}; no result accepted")
        if backend == "codex":
            answer_path = directory / "answer.json"
            if not answer_path.is_file():
                raise RuntimeError("codex model process returned no final message")
            answer = answer_path.read_text(encoding="utf-8")
        else:
            try:
                envelope = json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                raise ValueError(f"gemini CLI envelope is not JSON: {exc}") from exc
            if not isinstance(envelope, dict) or envelope.get("error") or not isinstance(envelope.get("response"), str):
                raise ValueError("gemini CLI returned an error or omitted its final response")
            answer = envelope["response"]
        result, error = extract_result(answer)
        if result is None:
            raise ValueError(error)
        return result
