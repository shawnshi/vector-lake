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

The child is a headless Pi session (``pi --print --no-session``) whose system prompt tells
it to delegate the ingest to the configured subagent and to print only the JSON array.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

PI_BIN = os.environ.get("VECTOR_LAKE_RUNNER_PI_BIN", "pi")
AGENT = os.environ.get("VECTOR_LAKE_RUNNER_SUBAGENT_AGENT", "reviewer")
TIMEOUT = int(os.environ.get("VECTOR_LAKE_RUNNER_MODEL_TIMEOUT", "1800"))

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
    try:
        argv = [pi_path, "--print", "--no-session", f"@{brief_path}"]
        if system_path:
            argv[3:3] = ["--append-system-prompt", system_path]
        # npm installs `pi` as a .CMD shim on Windows and CreateProcess cannot start that
        # directly (FileNotFoundError [WinError 2]).  Route it through the command
        # interpreter rather than shell=True, which would re-introduce quoting hazards.
        if os.name == "nt" and pi_path.lower().endswith((".cmd", ".bat")):
            argv = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *argv]
        proc = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=TIMEOUT, shell=False,
        )
    finally:
        for path in (brief_path, system_path):
            if not path:
                continue
            try:
                os.unlink(path)
            except OSError:
                pass

    if proc.returncode != 0:
        print(f"model seam: pi exited {proc.returncode}: {proc.stderr.strip()[:400]}", file=sys.stderr)
        return 4
    payload, error = _extract_array(proc.stdout)
    if payload is None:
        # Pi can exit 0 while explaining the problem on stderr, so surface both streams.
        print(f"model seam: {error}; stdout head: {proc.stdout[:300]!r}; "
              f"stderr head: {proc.stderr[:600]!r}", file=sys.stderr)
        return 5
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
