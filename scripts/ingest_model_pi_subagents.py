#!/usr/bin/env python
"""Model seam for the ingest runner: do the ingest work through pi-subagents.

Contract with the runner (``scripts/ingest_runner.py --model-cmd``):
  stdin   one task packet JSON (as emitted by ``claim_ingest_tasks``)
  stdout  the contract JSON array: ``[{"filename": ..., "content": ...}, ...]``
  exit 0  success; non-zero means "model failed" and the runner records it

Why a read-only pi-subagents profile: the runtime is the only writer (the runner submits
through ``finalize_ingest``, which owns validation, the schema gates and the canonical
store).  An ingest child therefore must read the raw document and *return text*; it must
never write wiki files itself.  ``reviewer`` (read/grep/find/ls) satisfies that;
``worker`` carries edit/write and would bypass the write path.

The child is a headless Pi session (``pi --print --session-dir <scratch/runner_sessions>``)
whose system prompt tells it to delegate the ingest to the configured subagent and to print
only the JSON array.

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
- Your entire final answer must be the JSON array the brief asks for: no prose, no
  markdown fence, no commentary before or after it.
- If the subagent's answer is not a valid JSON array, repair it into one yourself from the
  same evidence instead of returning an error.
- Keep the output small: ONE Source page, and the whole JSON array under 5000 characters.
  A compact page that closes properly is required; a truncated one is discarded and the
  task is wasted.  Aim for a 3-5 sentence summary plus 4-6 short bullets with inline
  (Source: [[...]]) anchors, and do not restate the source document.
""" % {"agent": AGENT}


def _extract_array(text: str):
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return None, "no JSON array in child output"
    try:
        payload = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        return None, f"child JSON invalid: {exc}"
    if not isinstance(payload, list) or not payload:
        return None, "child produced an empty payload"
    # The contract is [{filename, content}].  Children often also echo the packet's
    # ``processed_data`` as an extra element; that is discarded here (the host supplies
    # processed_data to finalize_ingest itself) instead of failing the whole task.
    files = [
        entry for entry in payload
        if isinstance(entry, dict) and "filename" in entry and "content" in entry
    ]
    if not files:
        return None, f"payload has no filename/content entry: {str(payload)[:160]}"
    return files, ""


def _brief(packet: dict) -> str:
    metadata = (packet.get("metadata") or {}).get("processed_data") or {}
    return (
        f"{packet.get('prompt', '')}\n\n"
        f"---\nExpected output: {packet.get('expected_output', 'JSON array of {filename, content}')}\n"
        f"The raw file to ingest is: {metadata.get('filepath')}\n"
        f"The page that MUST exist in your payload is: {metadata.get('canonical_name')}\n"
        "Return only the JSON array.\n"
    )


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
    payload, error = _extract_array(proc.stdout)
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
