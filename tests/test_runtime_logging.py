"""The durable rotating log that replaced stderr-only diagnostics."""

from __future__ import annotations

import logging
import os
import sys

import pytest

from vector_lake import runtime_logging


@pytest.fixture(autouse=True)
def _reset_runtime_logging():
    runtime_logging._reset_file_handlers_for_tests()
    yield
    runtime_logging._reset_file_handlers_for_tests()


def test_configure_creates_the_log_and_persists_records(isolated_memory):
    path = runtime_logging.configure_runtime_logging("unit-test")
    assert path
    assert os.path.isfile(path)

    logging.getLogger("vector-lake-auto-ingest").error("relay_response_timeout")
    for handler in logging.getLogger().handlers:
        handler.flush()

    contents = open(path, encoding="utf-8").read()
    assert "relay_response_timeout" in contents
    assert "Runtime logging started for component=unit-test" in contents


def test_configure_never_installs_a_stdout_handler(isolated_memory):
    """The MCP transport is stdio; a stdout handler would corrupt the protocol."""

    runtime_logging.configure_runtime_logging("mcp")

    status = runtime_logging.runtime_logging_status()
    assert status["stdout_handler"] is False
    assert status["process_has_handler"] is True
    assert status["configured"] is True


def test_configure_is_idempotent(isolated_memory):
    first = runtime_logging.configure_runtime_logging("first")
    handlers_after_first = len(logging.getLogger().handlers)
    second = runtime_logging.configure_runtime_logging("second")

    assert first == second
    assert len(logging.getLogger().handlers) == handlers_after_first

    logging.getLogger("vector-lake-scheduler").warning("deduplicated-handler")
    for handler in logging.getLogger().handlers:
        handler.flush()
    contents = open(first, encoding="utf-8").read()
    assert contents.count("deduplicated-handler") == 1


def test_rotation_bounds_are_configurable(monkeypatch):
    assert runtime_logging.configured_log_max_bytes() == 8 * 1024 * 1024
    assert runtime_logging.configured_log_backup_count() == 3

    monkeypatch.setenv("VECTOR_LAKE_LOG_MAX_BYTES", "65536")
    monkeypatch.setenv("VECTOR_LAKE_LOG_BACKUP_COUNT", "5")
    assert runtime_logging.configured_log_max_bytes() == 65_536
    assert runtime_logging.configured_log_backup_count() == 5

    monkeypatch.setenv("VECTOR_LAKE_LOG_MAX_BYTES", "0")
    monkeypatch.setenv("VECTOR_LAKE_LOG_BACKUP_COUNT", "999")
    assert runtime_logging.configured_log_max_bytes() == 1
    assert (
        runtime_logging.configured_log_backup_count()
        == runtime_logging._BACKUP_COUNT_CEILING
    )


def test_unwritable_log_target_reports_instead_of_killing_the_process(
    isolated_memory, monkeypatch
):
    def _deny(*_args, **_kwargs):
        raise OSError(13, "denied")

    monkeypatch.setattr(runtime_logging.Path, "mkdir", _deny)

    assert runtime_logging.configure_runtime_logging("denied") == ""
    # The process still has somewhere to report, so the failure is not silent.
    assert runtime_logging.runtime_logging_status()["configured"] is False


def test_log_target_lives_under_the_active_meta_directory(isolated_memory):
    path = runtime_logging.configure_runtime_logging("meta-dir-test")

    assert os.path.normcase(str(isolated_memory / "wiki" / ".meta")) in os.path.normcase(
        path
    )
    assert os.path.basename(path) == "vector-lake.log"


def test_moving_the_meta_directory_replaces_the_handler(tmp_path):
    """A stale handler would hold its file open for the life of the process.

    The CLI and MCP launchers call this once per process, but a session that
    changes meta directories (and every test that drives ``cli_app.main``) must
    not accumulate one open log file per destination.
    """
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()

    first = runtime_logging.configure_runtime_logging("one", meta_dir=first_root)
    second = runtime_logging.configure_runtime_logging("two", meta_dir=second_root)

    assert first != second
    assert runtime_logging.runtime_logging_status()["file_handlers"] == [second]
    assert len(runtime_logging._installed_file_handlers(logging.getLogger())) == 1

    logging.getLogger("vector-lake-watchdog").error("moved-destination")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "moved-destination" in open(second, encoding="utf-8").read()
    assert (
        "moved-destination" not in open(first, encoding="utf-8").read()
    )


def test_stderr_handler_follows_a_replaced_stream(tmp_path, monkeypatch):
    """A StreamHandler bound at construction breaks on stream replacement.

    The CLI and MCP launchers install logging once per process.  A test runner's
    capture, or a host that reopens stderr, replaces ``sys.stderr`` afterwards;
    a handler holding the old object then writes to a closed file and emits
    ``--- Logging error ---`` to stderr, which is worse than the log line it
    failed to write.
    """
    import io

    runtime_logging._reset_file_handlers_for_tests()
    for handler in list(logging.getLogger().handlers):
        logging.getLogger().removeHandler(handler)

    first = io.StringIO()
    monkeypatch.setattr(sys, "stderr", first)
    path = runtime_logging.configure_runtime_logging("stream-swap", meta_dir=tmp_path)
    assert path
    logging.getLogger("vector-lake-cli").info("before-swap")

    second = io.StringIO()
    monkeypatch.setattr(sys, "stderr", second)
    logging.getLogger("vector-lake-cli").info("after-swap")

    assert "before-swap" in first.getvalue(), first.getvalue()
    assert "after-swap" in second.getvalue(), second.getvalue()
    # The failure mode was writing the record to the closed stream and letting
    # logging print its own traceback to stderr.
    assert "Logging error" not in first.getvalue()
    assert "Logging error" not in second.getvalue()
