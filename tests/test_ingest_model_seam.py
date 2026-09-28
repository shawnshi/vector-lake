"""The model seam's child launch, and the failure evidence it keeps.

Two defects measured on 2026-09-25 are pinned here:

* the child ran with ``--no-session``.  That does not stop the ingest, but it removes the
  session root SoL-Pi and pi-subagents derive their own paths from, so every child printed
  ``SoL-Pi requires a persistent Pi session directory`` 8-11 times before doing any work;
* a failure reported only ``stderr[:400]``, while ``scripts/ingest_runner.py`` keeps only the
  first 300 characters of that.  The one recorded failure of that cycle was stored truncated
  mid-line (``Extension error (C:\\Users\\s``) with the real error already cut off, so the
  diagnosis was destroyed by the reporting rather than lost by the child.

Both are asserted against the code path that produces them, with ``subprocess.run`` replaced:
no pi process, no model call.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SOL_PI_BANNER = (
    "Extension error (C:\\Users\\shich\\.pi\\agent\\git\\github.com\\NVlabs\\SoL-Pi\\src"
    "\\sol-pi\\index.ts): SoL-Pi requires a persistent Pi session directory\n"
) * 8
REAL_ERROR = "provider error: 402 insufficient balance for this account"


def _load_seam():
    spec = importlib.util.spec_from_file_location(
        "seam_under_test", REPO / "scripts" / "ingest_model_pi_subagents.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def seam(tmp_path, monkeypatch):
    """Load the seam once and point both of its scratch trees at the test's tmp_path."""
    module = _load_seam()
    monkeypatch.setattr(module, "SCRATCH", tmp_path / "scratch")
    monkeypatch.setattr(module, "SESSION_DIR", tmp_path / "scratch" / "runner_sessions")
    return module


def _run(seam, monkeypatch, result=None, raises=None, packet=None):
    monkeypatch.setattr(seam.shutil, "which", lambda name: "C:/fake/pi")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(packet or {"prompt": "brief"})))

    def fake_run(argv, **kwargs):
        fake_run.argv = argv
        if raises is not None:
            raise raises
        return result

    monkeypatch.setattr(seam.subprocess, "run", fake_run)
    return fake_run


def _logs(seam):
    return sorted(seam.SCRATCH.glob("runner_model-*-err.log")) if seam.SCRATCH.exists() else []


def test_child_is_one_shot_but_still_has_a_session_root(seam):
    """--session-dir replaces --no-session without making the child resumable."""
    argv = seam._argv("pi", "brief.md", "system.md", seam.SESSION_DIR)

    assert "--no-session" not in argv
    assert argv[:4] == ["pi", "--print", "--session-dir", str(seam.SESSION_DIR)]
    assert argv[4:6] == ["--append-system-prompt", "system.md"]
    assert argv[-1] == "@brief.md", "the brief stays the last positional argument"


def test_windows_shim_is_routed_through_the_command_interpreter(seam, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")

    argv = seam._argv("C:\\npm\\pi.cmd", "brief.md", None, seam.SESSION_DIR)

    assert argv[:2] == [os.environ.get("COMSPEC", "cmd.exe"), "/c"]
    assert argv[2] == "C:\\npm\\pi.cmd"
    assert argv[-1] == "@brief.md"


def test_unwritable_scratch_is_not_a_new_failure_mode(seam, monkeypatch, capsys, tmp_path):
    """A read-only scratch must degrade to a temp session root, quietly and successfully."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(seam, "SESSION_DIR", blocker / "runner_sessions")
    payload = json.dumps({"files_written": [{"filename": "Source_x.md", "content": "# x"}],
                          "integration": {"disposition": "standalone", "reason": "No existing matching node."}})
    fake_run = _run(seam, monkeypatch, result=subprocess.CompletedProcess(["pi"], 0, payload, ""))

    assert seam.main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["files_written"][0]["filename"] == "Source_x.md"
    assert captured.err == "", "a succeeding run must not spend the runner's stderr budget"
    assert "--session-dir" in fake_run.argv
    assert fake_run.argv[fake_run.argv.index("--session-dir") + 1] != str(seam.SESSION_DIR)


def test_failed_child_is_reported_by_log_path_inside_the_runners_budget(seam, monkeypatch, capsys):
    """The runner truncates at 300 characters, so the path has to survive that cut."""
    result = subprocess.CompletedProcess(["pi"], 1, "", SOL_PI_BANNER + REAL_ERROR)
    _run(seam, monkeypatch, result=result)

    assert seam.main() == 4
    message = capsys.readouterr().err.strip()

    stored = f"model runner exited 4: {message}"[:300]
    assert "runner_model-" in stored, f"the log path was cut off: {stored!r}"
    log = Path(message.split("full log: ", 1)[1].split(";", 1)[0])
    assert not log.is_absolute() or log.exists()
    assert (seam.ROOT / log).read_text(encoding="utf-8").endswith(REAL_ERROR)
    assert REAL_ERROR in message, "the tail carries the diagnosis even when the head is noise"


def test_successful_child_writes_no_failure_log(seam, monkeypatch, capsys):
    payload = {"files_written": [{"filename": "Source_x.md", "content": "# x"}],
               "integration": {"disposition": "standalone", "reason": "No existing matching node."}}
    _run(seam, monkeypatch, result=subprocess.CompletedProcess(["pi"], 0, json.dumps(payload), ""))

    assert seam.main() == 0
    assert json.loads(capsys.readouterr().out) == payload
    assert _logs(seam) == []


@pytest.mark.parametrize("payload", [
    [{"filename": "Source_x.md", "content": "# x"}],
    {"files_written": [{"filename": "Source_x.md", "content": "# x"}]},
    {"files_written": [], "integration": {"disposition": "standalone", "reason": "No match."}},
    {"files_written": [{"filename": "Source_x.md", "content": "# x", "processed_data": {}}],
     "integration": {"disposition": "standalone", "reason": "No match."}},
    {"files_written": [{"filename": "Source_x.md", "content": "# x"}],
     "integration": {"disposition": "integrated"}, "processed_data": {"lease_token": "forged"}},
])
def test_seam_rejects_old_or_untrusted_output(seam, payload):
    parsed, error = seam._extract_result(json.dumps(payload))
    assert parsed is None and error


@pytest.mark.parametrize("disposition", ["rejected", "Rejected", " rejected "])
def test_seam_accepts_explicit_rejection(seam, disposition):
    payload = {"files_written": [], "integration": {"disposition": disposition, "reason": "The source is out of scope."}}
    assert seam._extract_result(json.dumps(payload)) == (payload, "")


@pytest.mark.parametrize("disposition,files", [
    ("integrated", [{"filename": "Source_x.md", "content": "# x"}]),
    ("standalone", [{"filename": "Source_x.md", "content": "# x"}]),
    ("Rejected", []),
])
def test_runner_passes_model_decision_without_replacing_host_versions(monkeypatch, disposition, files):
    from scripts import ingest_runner as runner

    integration = {"disposition": disposition}
    integration.update(
        {"relations": [{"target": "Concept_Target.md", "target_hash": "host-candidate"}]}
        if disposition == "integrated" else {"reason": "A complete auditable reason."}
    )
    result = {"files_written": files, "integration": integration}
    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(["model"], 0, json.dumps(result), ""))
    monkeypatch.setattr(runner, "classify", lambda *_: "needs-model")
    submitted = []
    monkeypatch.setattr(runner, "finalize_ingest", lambda f, p:
                        submitted.append((f, p)) or "Successfully finalized (isolated test)")
    processed = {"job_id": "synthetic", "lease_token": "host-lease", "source_hash": "host-source",
                 "integration_candidates": [{"target": "Concept_Target.md", "target_hash": "host-candidate"}]}
    task = {"job_id": "synthetic", "task_packet": {"metadata": {"processed_data": processed}}}

    stats, error = runner._process_task(task, False, "fake-model", {})

    assert not error and stats["finalized"] == 1
    assert submitted == [(files, {**processed, "integration": integration})]
    assert submitted[0][1]["lease_token"] == "host-lease"


@pytest.mark.parametrize("output", [
    json.dumps([{"filename": "Source_x.md", "content": "# x"}]),
    json.dumps({"files_written": [{"filename": "Source_x.md", "content": "# x"}]}),
    json.dumps({"files_written": [], "integration": {"disposition": "standalone"}}),
    "model timed out",
])
def test_runner_fails_closed_on_missing_or_malformed_decision(monkeypatch, output):
    from scripts import ingest_runner as runner

    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(["model"], 0, output, ""))
    monkeypatch.setattr(runner, "classify", lambda *_: "needs-model")
    failures = []
    monkeypatch.setattr(runner, "record_ingest_failure", lambda job, reason: failures.append((job, reason)))
    monkeypatch.setattr(runner, "finalize_ingest", lambda *_: pytest.fail("failed output reached finalizer"))
    task = {"job_id": "synthetic", "task_packet": {"metadata": {"processed_data": {"job_id": "synthetic"}}}}

    stats, error = runner._process_task(task, False, "fake-model", {})

    assert error and stats["model-failed"] == 1
    assert failures[0][0] == "synthetic"


def test_parse_failure_keeps_both_streams(seam, monkeypatch, capsys):
    """pi can exit 0 and explain itself on stderr; that is still a failure with evidence."""
    result = subprocess.CompletedProcess(["pi"], 0, "I could not do it", SOL_PI_BANNER + REAL_ERROR)
    _run(seam, monkeypatch, result=result)

    assert seam.main() == 5
    message = capsys.readouterr().err
    err_log = Path(message.split("full log: ", 1)[1].split(";", 1)[0])
    out_log = err_log.with_name(err_log.name.replace("-err.log", "-out.log"))
    assert "I could not do it" in (seam.ROOT / out_log).read_text(encoding="utf-8")
    assert REAL_ERROR in (seam.ROOT / err_log).read_text(encoding="utf-8")


def test_timeout_is_a_model_failure_not_a_traceback(seam, monkeypatch, capsys):
    argv = ["pi", "--print"]
    _run(seam, monkeypatch, raises=subprocess.TimeoutExpired(argv, 1, output="partial out", stderr="partial err"))

    assert seam.main() == 4
    message = capsys.readouterr().err
    assert "timed out" in message
    log = Path(message.split("full log: ", 1)[1].split(";", 1)[0])
    assert (seam.ROOT / log).read_text(encoding="utf-8") == "partial err"


def test_prune_keeps_newest_logs_and_only_aged_sessions(seam):
    import time

    seam.SCRATCH.mkdir(parents=True)
    now = time.time()
    for index in range(seam.KEEP_LOGS + 2):
        for stream in ("out", "err"):
            path = seam.SCRATCH / f"runner_model-20260925-0000{index:02d}-1-{stream}.log"
            path.write_text("x", encoding="utf-8")
            os.utime(path, (now - (seam.KEEP_LOGS + 2 - index) * 60,) * 2)
    seam.SESSION_DIR.mkdir(parents=True)
    aged = seam.SESSION_DIR / "2026-09-01T00-00-00-000Z_old.jsonl"
    aged.write_text("{}", encoding="utf-8")
    os.utime(aged, (now - (seam.SESSION_MAX_AGE_DAYS + 1) * 86400,) * 2)
    fresh = seam.SESSION_DIR / "2026-09-25T00-00-00-000Z_new.jsonl"
    fresh.write_text("{}", encoding="utf-8")

    seam._prune_scratch()

    for stream in ("out", "err"):
        kept = sorted(seam.SCRATCH.glob(f"runner_model-*-{stream}.log"))
        assert len(kept) == seam.KEEP_LOGS
        assert not (seam.SCRATCH / f"runner_model-20260925-000000-1-{stream}.log").exists()
        assert (seam.SCRATCH / f"runner_model-20260925-000021-1-{stream}.log").exists()
    assert fresh.exists()
    assert not aged.exists(), "an old session must not accumulate forever"
