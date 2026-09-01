"""Measure isolated multi-host Vector Lake MCP capacity and latency."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, TextIO

import psutil


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_VERSION = "2024-11-05"


class MCPProbe:
    """One isolated stdio MCP process with synchronous JSON-RPC probes."""

    def __init__(self, index: int, runtime_dir: Path, env: dict[str, str]):
        self.index = index
        self._next_request_id = 1
        self._stderr: TextIO = (runtime_dir / f"mcp-{index}.stderr.log").open(
            "w+",
            encoding="utf-8",
        )
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(ROOT / "scripts" / "vector_lake_mcp.py"),
                "--profile",
                "default",
                "--surface",
                "full",
            ],
            cwd=runtime_dir,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

    @property
    def pid(self) -> int:
        return self.process.pid

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
            }
        )

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_request_id
        self._next_request_id += 1
        self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        assert self.process.stdout is not None
        response_line = self.process.stdout.readline()
        if not response_line:
            self._stderr.flush()
            self._stderr.seek(0)
            detail = self._stderr.read()[-2000:]
            raise RuntimeError(
                f"MCP process {self.index} closed stdout before response: {detail}"
            )
        response = json.loads(response_line)
        if response.get("id") != request_id:
            raise RuntimeError(
                f"MCP process {self.index} returned an unexpected response id"
            )
        if response.get("error") is not None:
            raise RuntimeError(
                f"MCP process {self.index} request failed: {response['error']}"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(
                f"MCP process {self.index} returned a non-object result"
            )
        return result

    def initialize(self) -> tuple[float, int]:
        started = time.perf_counter()
        initialized = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": f"vector-lake-capacity-probe-{self.index}",
                    "version": "1",
                },
            },
        )
        if initialized.get("protocolVersion") != PROTOCOL_VERSION:
            raise RuntimeError("MCP protocol negotiation returned an unexpected version")
        self.notify("notifications/initialized", {})
        listed = self.request("tools/list", {})
        tools = listed.get("tools")
        if not isinstance(tools, list):
            raise RuntimeError("MCP tools/list did not return a tool array")
        return (time.perf_counter() - started) * 1000.0, len(tools)

    def runtime_status(self) -> tuple[float, dict[str, Any]]:
        started = time.perf_counter()
        result = self.request(
            "tools/call",
            {
                "name": "mcp_runtime_status",
                "arguments": {},
            },
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        content = result.get("content")
        if not isinstance(content, list) or not content:
            raise RuntimeError("mcp_runtime_status returned no content")
        text = content[0].get("text") if isinstance(content[0], dict) else None
        if not isinstance(text, str):
            raise RuntimeError("mcp_runtime_status returned non-text content")
        return elapsed_ms, json.loads(text)

    def _write(self, payload: dict[str, Any]) -> None:
        if self.process.poll() is not None:
            raise RuntimeError(
                f"MCP process {self.index} exited with {self.process.returncode}"
            )
        assert self.process.stdin is not None
        self.process.stdin.write(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        self.process.stdin.flush()

    def close(self) -> None:
        if self.process.poll() is None and self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)
        self._stderr.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Launch isolated full-surface MCP processes plus one watchdog and "
            "measure startup, RSS, and mcp_runtime_status latency."
        )
    )
    parser.add_argument("--mcp-processes", type=int, default=3)
    parser.add_argument("--duration-seconds", type=float, default=60.0)
    parser.add_argument("--max-total-rss-mib", type=float, default=1024.0)
    parser.add_argument("--max-runtime-status-p95-ms", type=float, default=250.0)
    parser.add_argument("--max-mcp-startup-ms", type=float, default=5000.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep-temp", action="store_true")
    return parser


def _validated_args(args: argparse.Namespace) -> None:
    if not 1 <= args.mcp_processes <= 8:
        raise ValueError("--mcp-processes must be between 1 and 8")
    if not math.isfinite(args.duration_seconds) or not 5 <= args.duration_seconds <= 3600:
        raise ValueError("--duration-seconds must be between 5 and 3600")
    for name in (
        "max_total_rss_mib",
        "max_runtime_status_p95_ms",
        "max_mcp_startup_ms",
    ):
        value = float(getattr(args, name))
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"--{name.replace('_', '-')} must be a positive number")


def _isolated_env(memory_dir: Path, meta_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "VECTOR_LAKE_MEMORY_DIR": str(memory_dir),
            "VECTOR_LAKE_META_DIR": str(meta_dir),
            "VECTOR_LAKE_DB_PATH": str(meta_dir / "vector_lake.db"),
            "VECTOR_LAKE_OPERATIONAL_MEMORY_FTS": "1",
            "VECTOR_LAKE_DURABILITY_PROFILE": "full",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    for name in (
        "PYTHONPATH",
        "VECTOR_LAKE_ENV_FILE",
        "VECTOR_LAKE_DIARY_SYNC_SCRIPT",
        "VECTOR_LAKE_RUNTIME_PROFILE_PATH",
    ):
        env.pop(name, None)
    return env


def _start_watchdog(
    runtime_dir: Path,
    env: dict[str, str],
    meta_dir: Path,
) -> tuple[subprocess.Popen, TextIO, float]:
    log_handle = (runtime_dir / "watchdog.log").open("w+", encoding="utf-8")
    started = time.perf_counter()
    process = subprocess.Popen(
        [sys.executable, "-B", str(ROOT / "watchdog_sync.py")],
        cwd=runtime_dir,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=log_handle,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    status_path = meta_dir / ".watchdog_status.json"
    deadline = time.monotonic() + 30.0
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                log_handle.flush()
                log_handle.seek(0)
                raise RuntimeError(
                    f"watchdog exited with {process.returncode}: "
                    + log_handle.read()[-2000:]
                )
            if status_path.exists():
                try:
                    status = json.loads(status_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    status = {}
                if status.get("status") in {"idle", "processing"}:
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    return process, log_handle, elapsed_ms
            time.sleep(0.05)
        raise RuntimeError("watchdog did not publish a ready status within 30 seconds")
    except BaseException:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        log_handle.close()
        raise


def _stop_watchdog(
    process: subprocess.Popen | None,
    log_handle: TextIO | None,
    meta_dir: Path,
) -> None:
    if process is not None and process.poll() is None:
        (meta_dir / ".watchdog.stop").touch()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
    if log_handle is not None:
        log_handle.close()


def _rss_snapshot(processes: list[subprocess.Popen]) -> dict[int, int]:
    snapshot: dict[int, int] = {}
    for process in processes:
        if process.poll() is not None:
            continue
        root = psutil.Process(process.pid)
        rss_bytes = root.memory_info().rss
        for child in root.children(recursive=True):
            try:
                rss_bytes += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        snapshot[process.pid] = rss_bytes
    return snapshot


def _nearest_rank_percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def run_benchmark(args: argparse.Namespace, runtime_dir: Path) -> dict[str, Any]:
    memory_dir = runtime_dir / "MEMORY"
    meta_dir = memory_dir / "wiki" / ".meta"
    meta_dir.mkdir(parents=True)
    env = _isolated_env(memory_dir, meta_dir)
    probes: list[MCPProbe] = []
    watchdog: subprocess.Popen | None = None
    watchdog_log: TextIO | None = None
    startup_ms: list[float] = []
    tool_counts: list[int] = []
    status_latencies_ms: list[float] = []
    status_samples: list[dict[str, Any]] = []
    rss_samples: list[dict[int, int]] = []
    watchdog_startup_ms = 0.0

    try:
        for index in range(args.mcp_processes):
            probe = MCPProbe(index, runtime_dir, env)
            probes.append(probe)
            elapsed_ms, tool_count = probe.initialize()
            startup_ms.append(elapsed_ms)
            tool_counts.append(tool_count)
        watchdog, watchdog_log, watchdog_startup_ms = _start_watchdog(
            runtime_dir,
            env,
            meta_dir,
        )
        processes = [probe.process for probe in probes] + [watchdog]
        time.sleep(2.0)
        rss_samples.append(_rss_snapshot(processes))
        deadline = time.monotonic() + args.duration_seconds
        while time.monotonic() < deadline:
            for probe in probes:
                latency_ms, status = probe.runtime_status()
                status_latencies_ms.append(latency_ms)
                status_samples.append(status)
            rss_samples.append(_rss_snapshot(processes))
            time.sleep(0.25)

        expected_pids = {process.pid for process in processes}
        if any(set(sample) != expected_pids for sample in rss_samples):
            raise RuntimeError("one or more measured processes exited during the soak")
        peak_by_pid = {
            pid: max(sample[pid] for sample in rss_samples)
            for pid in expected_pids
        }
        final_by_pid = rss_samples[-1]
        total_peak_bytes = max(sum(sample.values()) for sample in rss_samples)
        p95_ms = _nearest_rank_percentile(status_latencies_ms, 0.95)
        stale_reports = sum(
            1
            for status in status_samples
            if bool((status.get("runtime_revision") or {}).get("stale"))
        )
        thresholds = {
            "total_peak_rss_mib": total_peak_bytes / (1024 * 1024)
            <= args.max_total_rss_mib,
            "runtime_status_p95_ms": p95_ms
            <= args.max_runtime_status_p95_ms,
            "mcp_startup_ms": max(startup_ms) <= args.max_mcp_startup_ms,
            "tool_count": all(count == 64 for count in tool_counts),
            "runtime_stale_reports": stale_reports == 0,
        }
        return {
            "schema_version": 1,
            "passed": all(thresholds.values()),
            "thresholds": thresholds,
            "configuration": {
                "mcp_processes": args.mcp_processes,
                "mcp_surface": "full",
                "watchdog_processes": 1,
                "duration_seconds": args.duration_seconds,
                "max_total_rss_mib": args.max_total_rss_mib,
                "max_runtime_status_p95_ms": args.max_runtime_status_p95_ms,
                "max_mcp_startup_ms": args.max_mcp_startup_ms,
            },
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "runtime_dir": str(runtime_dir),
                "source_root": str(ROOT),
            },
            "mcp": {
                "tool_counts": tool_counts,
                "startup_ms": [round(value, 3) for value in startup_ms],
                "startup_max_ms": round(max(startup_ms), 3),
                "runtime_status_calls": len(status_latencies_ms),
                "runtime_status_p50_ms": round(
                    statistics.median(status_latencies_ms),
                    3,
                ),
                "runtime_status_p95_ms": round(p95_ms, 3),
                "runtime_status_max_ms": round(max(status_latencies_ms), 3),
                "runtime_stale_reports": stale_reports,
            },
            "watchdog": {
                "startup_ms": round(watchdog_startup_ms, 3),
            },
            "memory": {
                "steady_rss_mib_by_pid": {
                    str(pid): round(value / (1024 * 1024), 3)
                    for pid, value in sorted(final_by_pid.items())
                },
                "peak_rss_mib_by_pid": {
                    str(pid): round(value / (1024 * 1024), 3)
                    for pid, value in sorted(peak_by_pid.items())
                },
                "total_steady_rss_mib": round(
                    sum(final_by_pid.values()) / (1024 * 1024),
                    3,
                ),
                "total_peak_rss_mib": round(
                    total_peak_bytes / (1024 * 1024),
                    3,
                ),
            },
        }
    finally:
        for probe in probes:
            probe.close()
        _stop_watchdog(watchdog, watchdog_log, meta_dir)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        _validated_args(args)
    except ValueError as exc:
        parser.error(str(exc))

    if args.keep_temp:
        runtime_dir = Path(tempfile.mkdtemp(prefix="vector-lake-capacity-"))
        cleanup = False
    else:
        runtime_dir = Path(tempfile.mkdtemp(prefix="vector-lake-capacity-"))
        cleanup = True
    try:
        result = run_benchmark(args, runtime_dir)
        rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
        if args.output is not None:
            output_path = args.output.expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 0 if result["passed"] else 2
    finally:
        if cleanup:
            shutil.rmtree(runtime_dir, ignore_errors=True)
        else:
            print(f"Benchmark runtime retained at: {runtime_dir}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
