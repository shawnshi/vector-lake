"""Durable rotating log for long-running Vector Lake processes.

Every module configured logging through ``logging.basicConfig``, whose first call
wins and which the MCP server then reset with ``force=True``.  Nothing ever wrote
a file: no ``*.log`` existed anywhere under the active meta directory, so a
watchdog that had been failing every attempt for days left no trace at all and
the failures were only visible as stale JSON state.  This module installs one
bounded rotating file handler plus a stderr handler, is safe to call more than
once, and never touches stdout so the stdio MCP transport stays clean.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from vector_lake.wiki_utils import peek_meta_dir

_LOG_DIR_NAME = "logs"
_LOG_FILE_NAME = "vector-lake.log"
_MAX_BYTES_DEFAULT = 8 * 1024 * 1024
_MAX_BYTES_CEILING = 512 * 1024 * 1024
_BACKUP_COUNT_DEFAULT = 3
_BACKUP_COUNT_CEILING = 20
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _file_handler_key(handler: RotatingFileHandler) -> str:
    return os.path.normcase(os.path.abspath(str(getattr(handler, "baseFilename", ""))))


_installed_handlers: list[logging.Handler] = []


def _installed_file_handlers(logger: logging.Logger) -> list[RotatingFileHandler]:
    return [
        handler
        for handler in logger.handlers
        if isinstance(handler, RotatingFileHandler)
    ]


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def configured_log_max_bytes() -> int:
    return _bounded_env_int(
        "VECTOR_LAKE_LOG_MAX_BYTES", _MAX_BYTES_DEFAULT, 1, _MAX_BYTES_CEILING
    )


def configured_log_backup_count() -> int:
    return _bounded_env_int(
        "VECTOR_LAKE_LOG_BACKUP_COUNT",
        _BACKUP_COUNT_DEFAULT,
        0,
        _BACKUP_COUNT_CEILING,
    )


def runtime_log_path(meta_dir: str | os.PathLike[str] | None = None) -> Path:
    root = Path(meta_dir) if meta_dir is not None else Path(peek_meta_dir())
    return root / _LOG_DIR_NAME / _LOG_FILE_NAME


def _root_logger() -> logging.Logger:
    return logging.getLogger()


class _CurrentStderrHandler(logging.StreamHandler):
    """A stderr handler that follows the live ``sys.stderr``.

    ``logging.StreamHandler()`` binds ``sys.stderr`` at construction time.  A
    process that installs the handler once and then has stderr replaced (a test
    runner's capture, or a host that reopens the stream) would log to a closed
    file and emit ``--- Logging error ---`` to the very stream it meant to write
    to.  Refreshing the stream per record costs one assignment and removes the
    whole class of failure.
    """

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


def _has_logging_handler(logger: logging.Logger) -> bool:
    """True when the process already has somewhere to write records.

    Deliberately permissive: a host or test runner that installed its own
    handlers must not get a second, competing one, and installing our own
    ``StreamHandler`` is exactly the construction-time stream binding described
    in :class:`_CurrentStderrHandler`.
    """
    return bool(logger.handlers)


def _owns_stderr_handler(logger: logging.Logger) -> bool:
    return any(
        isinstance(handler, _CurrentStderrHandler) for handler in logger.handlers
    )


def _reset_file_handlers_for_tests() -> None:
    """Detach and close every handler this module installed."""
    logger = _root_logger()
    for handler in list(_installed_handlers):
        logger.removeHandler(handler)
        handler.close()
    _installed_handlers.clear()


def configure_runtime_logging(
    component: str = "runtime",
    *,
    meta_dir: str | os.PathLike[str] | None = None,
) -> str:
    """Install the durable log handlers and return the log path.

    Returns ``""`` when the log file cannot be created.  A process that cannot
    open its own log must still start and serve, so the failure is reported to
    the caller instead of raised; the caller decides whether that is fatal.
    """
    target = runtime_log_path(meta_dir)
    logger = _root_logger()
    if logger.level > logging.INFO or logger.level == logging.NOTSET:
        logger.setLevel(logging.INFO)
    if not _has_logging_handler(logger):
        stderr_handler = _CurrentStderrHandler()
        stderr_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        logger.addHandler(stderr_handler)
        _installed_handlers.append(stderr_handler)
    key = os.path.normcase(os.path.abspath(str(target)))
    existing = _installed_file_handlers(logger)
    if existing:
        if len(existing) == 1 and _file_handler_key(existing[0]) == key:
            return str(target)
        # The destination moved (a test's temporary root, or an operator
        # switching meta directories).  Dropping the stale handler releases its
        # file handle instead of leaking one per destination for the life of the
        # process.
        for handler in existing:
            logger.removeHandler(handler)
            handler.close()
            if handler in _installed_handlers:
                _installed_handlers.remove(handler)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            str(target),
            maxBytes=configured_log_max_bytes(),
            backupCount=configured_log_backup_count(),
            encoding="utf-8",
            delay=True,
        )
    except OSError as exc:
        logging.getLogger("vector-lake-runtime-logging").warning(
            "Runtime file logging is unavailable at %s: %s: %s",
            target,
            type(exc).__name__,
            exc,
        )
        return ""
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)
    _installed_handlers.append(handler)
    logging.getLogger(f"vector-lake-{component}").info(
        "Runtime logging started for component=%s at %s", component, target
    )
    return str(target)


def runtime_logging_status() -> dict[str, Any]:
    """Report the resolved log target and the installed handlers."""
    logger = _root_logger()
    file_paths = [
        str(getattr(handler, "baseFilename", ""))
        for handler in _installed_file_handlers(logger)
    ]
    return {
        "path": str(runtime_log_path()),
        "configured": bool(file_paths),
        "file_handlers": file_paths,
        "max_bytes": configured_log_max_bytes(),
        "backup_count": configured_log_backup_count(),
        "process_has_handler": bool(logger.handlers),
        "owns_stderr_handler": _owns_stderr_handler(logger),
        "stdout_handler": any(
            type(handler) is logging.StreamHandler
            and getattr(getattr(handler, "stream", None), "name", "") in
            {"<stdout>", "stdout"}
            for handler in logger.handlers
        ),
    }


__all__ = [
    "configure_runtime_logging",
    "configured_log_backup_count",
    "configured_log_max_bytes",
    "runtime_log_path",
    "runtime_logging_status",
]
