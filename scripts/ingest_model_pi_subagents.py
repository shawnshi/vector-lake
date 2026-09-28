#!/usr/bin/env python
"""Model seam for the ingest runner: do the ingest work through pi-subagents.

Contract with the runner (``scripts/ingest_runner.py --model-cmd``):
  stdin   one task packet JSON (as emitted by ``claim_ingest_tasks``)
  stdout  ``{"files_written": [{"filename": ..., "content": ...}], "integration": {...}}``
  exit 0  success; non-zero means "model failed" and the runner records it

Why a read-only pi-subagents profile: the runtime is the only writer (the runner submits
through ``finalize_ingest``, which owns validation, the schema gates and the canonical
store).  An ingest child therefore must read the raw document and *return text*; it must
never write wiki files itself.  ``reviewer`` (read/grep/find/ls) satisfies that;
``worker`` carries edit/write and would bypass the write path.

The child is a headless Pi session (``pi --print --session-dir <scratch/runner_sessions>``)
whose system prompt tells it to delegate the ingest to the configured subagent and to print
only the JSON object.

``--no-session`` used to be what made the child ephemeral, but it also removes the session
root that two extensions derive their own paths from.  Measured 2026-09-25: every child then
printed ``SoL-Pi requires a persistent Pi session directory`` 8-11 times before doing anything
(non-fatal, but ~1 KB of warnings that the runner's 300-character error budget cannot survive),
and pi-subagents fell back to the shared temp tree for child sessions and artifacts instead of
this seam's own tree.  An isolated session directory keeps the run one-shot while giving both
extensions a real root; it is pruned by age.

Failure evidence is preserved rather than truncated: ``scripts/ingest_runner.py`` keeps only the
first 300 characters of this process's stderr, which one ``pi`` startup banner already exceeds,
so a failure is written whole to ``scratch/runner_model-*-{out,err}.log`` and the message the
runner stores carries that path.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PI_BIN = os.environ.get("VECTOR_LAKE_RUNNER_PI_BIN", "pi")
AGENT = os.environ.get("VECTOR_LAKE_RUNNER_SUBAGENT_AGENT", "reviewer")
TIMEOUT = int(os.environ.get("VECTOR_LAKE_RUNNER_MODEL_TIMEOUT", "1800"))

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "scratch"
# Deliberately not an environment switch: the repo root already locates scratch, and a new
# VECTOR_LAKE_* literal would have to be registered in the README configuration section
# (``tests/test_registries.py``) to stay honest.
SESSION_DIR = SCRATCH / "runner_sessions"
KEEP_LOGS = 20
SESSION_MAX_AGE_DAYS = 3.0
FAILURE_TAIL_CHARS = 180

SYSTEM_PROMPT = """You are the ingest worker for a Vector Lake knowledge graph.

Rules that are not negotiable:
- You must delegate the actual ingest to the subagent tool: subagent({agent: "%(agent)s", task: <the ingest brief>}).
- Do NOT write, edit or delete any file. The host commits the result through
  finalize_ingest; a file you write yourself would bypass validation.
- Your entire final answer must be one JSON object with exactly `files_written` (array of
  filename/content objects) and `integration` (explicit disposition and its reason or relations).
  No prose, markdown fence, or extra fields such as processed_data. Do not call finalize_ingest.
- If the subagent returns invalid JSON or omits the semantic decision, report the failure;
  never invent a standalone disposition to make a malformed output pass.
- Keep the output compact: one canonical Source page is mandatory unless rejected. Aim for a
  3-5 sentence summary plus 4-6 short bullets with inline (Source: [[...]]) anchors, and do
  not restate the source document.
""" % {"agent": AGENT}


def _extract_result(text: str):
    try:
        payload = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        return None, f"child JSON invalid: {exc}"
    if not isinstance(payload, dict) or set(payload) != {"files_written", "integration"}:
        return None, "child must return files_written and integration only"
    files = payload["files_written"]
    integration = payload["integration"]
    if not isinstance(files, list) or not all(
        isinstance(item, dict) and set(item) == {"filename", "content"}
        and isinstance(item["filename"], str) and isinstance(item["content"], str)
        for item in files
    ):
        return None, "child files_written must contain filename/content objects only"
    if not isinstance(integration, dict) or not str(integration.get("disposition") or "").strip():
        return None, "child requires an explicit integration disposition"
    if not files and str(integration["disposition"]).strip().lower() != "rejected":
        return None, "child returned no files for a non-rejected disposition"
    return payload, ""


def _repair_block(repair: dict) -> str:
    """One bounded correction round: the finalizer's own message plus the answer it rejected."""
    error = " ".join(str(repair.get("validation_error") or "").split())[:1200]
    try:
        previous = json.dumps(repair.get("previous_output"), ensure_ascii=False)[:4000]
    except (TypeError, ValueError):
        previous = ""
    return (
        "\n--- REPAIR ROUND ---\n"
        "Your previous answer was rejected by the finalizer. Its message, verbatim:\n"
        f"{error}\n"
        + (f"\nThe answer that was rejected:\n{previous}\n" if previous else "")
        + "Return a corrected JSON object. Change only what the rejection requires, keep every\n"
        "field the contract lists, and do not drop or reword the file contents.\n"
    )


def _brief(packet: dict) -> str:
    metadata = (packet.get("metadata") or {}).get("processed_data") or {}
    return (
        f"{packet.get('prompt', '')}\n\n"
        f"---\nExpected output: JSON object with files_written and integration.\n"
        f"The raw file to ingest is: {metadata.get('filepath')}\n"
        f"The page that MUST exist in your payload is: {metadata.get('canonical_name')}\n"
        "Return only the JSON object. Do not return processed_data or call finalize_ingest.\n"
        f"\n{_packet_contract(packet)}"
        f"{_packet_repair(packet)}"
    )


def _packet_contract(packet: dict) -> str:
    """The packet carries its own contract; fall back to the module copy for older packets.

    Appended last on purpose: it is the final thing the child reads before answering, which is
    the opposite of the ~50 KB prompt's middle where the same rules used to live.
    """
    stored = str((packet.get("metadata") or {}).get("output_contract") or "").strip()
    if stored:
        return stored
    # Pre-v9 packet.  This seam deliberately does not import ``vector_lake``: it runs as a bare
    # script whose ``sys.path[0]`` is ``scripts/``, and the contract's one owner is
    # ``vector_lake.output_contract`` -- which the packet publisher has already used by the time
    # any packet reaches here.
    return (
        "OUTPUT CONTRACT: this packet carries none (it was built before v9). Follow the ingest "
        "prompt's field rules exactly: copy `target`, `target_hash` and `target_projection_hash` "
        "verbatim from `integration_candidates`, keep `confidence` a JSON number and "
        "`event_date` a plain YYYY-MM-DD.\n"
    )


def _packet_repair(packet: dict) -> str:
    repair = (packet.get("metadata") or {}).get("repair")
    return _repair_block(repair) if isinstance(repair, dict) else ""


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _prune_scratch() -> None:
    """Keep the runner's own scratch bounded: newest logs, sessions by age.

    Sessions are pruned by age rather than by count so a concurrently running child is
    never deleted underneath itself.
    """
    try:
        for stream in ("out", "err"):
            stale = sorted(
                SCRATCH.glob(f"runner_model-*-{stream}.log"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )[KEEP_LOGS:]
            for path in stale:
                path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        entries = list(SESSION_DIR.iterdir())
    except OSError:
        return
    cutoff = time.time() - SESSION_MAX_AGE_DAYS * 86400
    for entry in entries:
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            continue


def _argv(pi_path: str, brief_path: str, system_path: str | None, session_dir: Path) -> list[str]:
    argv = [pi_path, "--print", "--session-dir", str(session_dir)]
    if system_path:
        argv += ["--append-system-prompt", system_path]
    argv.append(f"@{brief_path}")
    # npm installs `pi` as a .CMD shim on Windows and CreateProcess cannot start that
    # directly (FileNotFoundError [WinError 2]).  Route it through the command
    # interpreter rather than shell=True, which would re-introduce quoting hazards.
    if os.name == "nt" and pi_path.lower().endswith((".cmd", ".bat")):
        argv = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *argv]
    return argv


def _as_text(value) -> str:
    if not value:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _persist_failure(stdout: str, stderr: str) -> str:
    """Write the child's complete output to scratch; return its log path ('' if unwritable)."""
    stem = f"runner_model-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    try:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        (SCRATCH / f"{stem}-out.log").write_text(stdout or "", encoding="utf-8")
        err_path = SCRATCH / f"{stem}-err.log"
        err_path.write_text(stderr or "", encoding="utf-8")
    except OSError:
        return ""
    return _relative(err_path)


def _failure_message(summary: str, stdout: str, stderr: str) -> str:
    """Keep the diagnosis inside the runner's 300-character budget.

    The path is placed before the tail on purpose: the runner truncates from the right, so
    a message that leads with the log location always tells a later reader where to look.
    """
    log = _persist_failure(stdout, stderr)
    tail = " ".join((stderr or "").split())[-FAILURE_TAIL_CHARS:]
    parts = [summary]
    if log:
        parts.append(f"full log: {log}")
    if tail:
        parts.append(f"stderr tail: {tail}")
    return "; ".join(parts)


def main() -> int:
    # Windows consoles default to a legacy codepage; the runner captures this
    # process over a pipe, so an un-reconfigured stdout raises UnicodeEncodeError
    # on any non-ASCII payload and the task silently fails as "model failed".
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    raw = sys.stdin.read()
    try:
        packet = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"model seam: invalid packet on stdin: {exc}", file=sys.stderr)
        return 2
    pi_path = shutil.which(PI_BIN)
    if not pi_path:
        print(f"model seam: pi binary {PI_BIN!r} not found on PATH", file=sys.stderr)
        return 3

    brief = _brief(packet)
    # The rules go to a file and are appended (not passed inline): a long
    # ``--system-prompt`` argument containing newlines is mangled by cmd.exe, which
    # silently produced an empty answer with exit 0.
    system_path = None
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(brief)
        brief_path = handle.name
    try:
        handle = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
        handle.write(SYSTEM_PROMPT)
        handle.close()
        system_path = handle.name
    except OSError:
        system_path = None
    session_dir = SESSION_DIR
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # An unwritable scratch must not become a new failure mode.  Fall back to a per-run
        # temp session root and keep ingesting; nothing is written to stderr, because noise on
        # a run that is succeeding would spend the runner's 300-character error budget.
        session_dir = Path(tempfile.mkdtemp(prefix="runner_sessions-"))
    _prune_scratch()
    try:
        proc = subprocess.run(
            _argv(pi_path, brief_path, system_path, session_dir),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=TIMEOUT, shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        # A hung child is a model failure with evidence, not a traceback.
        print(
            "model seam: "
            + _failure_message(
                f"pi timed out after {TIMEOUT}s",
                _as_text(getattr(exc, "stdout", "")),
                _as_text(getattr(exc, "stderr", "")),
            ),
            file=sys.stderr,
        )
        return 4
    finally:
        for path in (brief_path, system_path):
            if not path:
                continue
            try:
                os.unlink(path)
            except OSError:
                pass

    if proc.returncode != 0:
        print(
            "model seam: "
            + _failure_message(f"pi exited {proc.returncode}", proc.stdout, proc.stderr),
            file=sys.stderr,
        )
        return 4
    payload, error = _extract_result(proc.stdout)
    if payload is None:
        # Pi can exit 0 while explaining the problem on stderr, so surface both streams.
        print(
            "model seam: "
            + _failure_message(
                f"{error}; stdout head: {proc.stdout[:200]!r}", proc.stdout, proc.stderr
            ),
            file=sys.stderr,
        )
        return 5
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
