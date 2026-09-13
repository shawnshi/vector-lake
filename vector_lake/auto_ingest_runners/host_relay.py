"""Data-only runner adapter using a pinned, atomic filesystem spool."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from vector_lake.durability import durable_replace_file, sync_open_file

from .base import GenerationRequest, RunnerHandle, RunnerOptions, RunnerSafetyContract
from .codex_exec import _gate


RELAY_PROTOCOL_VERSION = "vector-lake-ingest-relay/v1"
EXPECTED_OUTPUT = "JSON array consumable by finalize_ingest(files_written, processed_data)"
# External usage is never measured or trusted: charge the entire reservation.
FULL_RESERVATION_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
# Fallback only, for a config object that lacks ``max_tokens_per_task``.  It must
# track ``auto_ingest_worker._MAX_TOKENS_PER_TASK`` (pinned by
# tests/test_auto_ingest_worker.py) and cannot import it here because the worker
# imports this adapter.
DEFAULT_FULL_RESERVATION_TOKENS = 262144
_OPTION_KEYS = frozenset({"spool_dir", "relay_protocol_version", "poll_seconds"})
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_FAILURE_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_REPARSE_POINT = 0x400


def _worker_type(name: str) -> type[Exception] | type[dict[str, Any]]:
    from vector_lake import auto_ingest_worker

    return getattr(auto_ingest_worker, name)


def _policy(code: str) -> Exception:
    return _worker_type("AutoIngestPolicyError")(code)  # type: ignore[call-arg]


def _infra(code: str) -> Exception:
    return _worker_type("AutoIngestInfrastructureError")(code)  # type: ignore[call-arg]


def _is_link_or_junction(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    try:
        if os.name == "nt" and not hasattr(info, "st_file_attributes"):
            return True
        attributes = int(getattr(info, "st_file_attributes", 0))
        return stat.S_ISLNK(info.st_mode) or bool(attributes & _REPARSE_POINT)
    except (OSError, TypeError, ValueError):
        return True


def _directory_identity(path: Path, code: str) -> tuple[int, int]:
    try:
        info = path.stat()
    except OSError:
        raise _infra(code) from None
    return (info.st_dev, info.st_ino)


def _validated_directory(path: Path, code: str) -> Path:
    if not path.is_absolute():
        raise ValueError(code)
    lexical = Path(os.path.abspath(path))
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        raise ValueError(code) from None
    if _is_link_or_junction(path) or not path.is_dir() or resolved != lexical:
        raise ValueError(code)
    return resolved


def _revalidate_pinned_directory(
    path: Path, identity: tuple[int, int], code: str
) -> Path:
    """Fail closed unless a previously canonicalized directory is still pinned."""
    try:
        if _is_link_or_junction(path):
            raise OSError
        resolved = path.resolve(strict=True)
        if (
            resolved != path
            or not resolved.is_dir()
            or _directory_identity(resolved, code) != identity
        ):
            raise OSError
    except OSError:
        raise _infra(code) from None
    return resolved


def _supports_handle_relative_io() -> bool:
    """Report only the complete primitive set needed for safe relative cleanup."""
    return (
        os.open in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.stat in os.supports_follow_symlinks
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
    )


def _safe_unlink(
    candidate: Path, pinned_directory: Path, identity: tuple[int, int]
) -> None:
    """Delete only a plain file still contained by its revalidated directory."""
    try:
        directory = _revalidate_pinned_directory(
            pinned_directory, identity, "relay_spool_subdirectory_changed"
        )
        if candidate.parent != directory or _is_link_or_junction(candidate):
            return
        try:
            if candidate.resolve(strict=True).parent != directory:
                return
        except FileNotFoundError:
            return
        except OSError:
            return
        if not _supports_handle_relative_io():
            # Deliberate availability-for-safety trade: cleanup never guesses.
            return
        directory_fd = os.open(
            directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            if (os.fstat(directory_fd).st_dev, os.fstat(directory_fd).st_ino) != identity:
                return
            info = os.stat(candidate.name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                return
            directory_info = os.fstat(directory_fd)
            if (directory_info.st_dev, directory_info.st_ino) != identity:
                return
            os.unlink(candidate.name, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        # Cleanup is best-effort; any failed safety check leaves the file in place.
        return


def _enforce_relay_protocol_version(value: str) -> None:
    if value != RELAY_PROTOCOL_VERSION:
        raise ValueError("relay_protocol_version_unsupported")


def _enforce_relay_response_binding(
    document: Mapping[str, Any], request: GenerationRequest, nonce: str
) -> None:
    if (
        document.get("protocol") != RELAY_PROTOCOL_VERSION
        or document.get("attempt_id") != request.attempt_id
        or document.get("lease_generation") != request.lease[2]
        or document.get("nonce") != nonce
    ):
        raise _infra("relay_response_binding_mismatch")


class HostRelayAdapter:
    safety_contract = RunnerSafetyContract(
        binary_pin=(
            "not_applicable",
            "No executable is spawned because the relay protocol is data-only.",
        ),
        version_pin=_gate(
            "_enforce_relay_protocol_version",
            "relay_protocol_version_unsupported",
            _enforce_relay_protocol_version,
        ),
        dedicated_home=(
            "not_applicable",
            "The data-only relay has no runner home.",
        ),
        instruction_surface_prohibited=(
            "not_applicable",
            "Compensating controls are the pinned spool root, data-only response, downstream purpose contract, and output validation.",
        ),
        user_skills_prohibited=(
            "not_applicable",
            "The data-only relay has no skills tree.",
        ),
        identity_pin=_gate(
            "_enforce_relay_response_binding",
            "relay_response_binding_mismatch",
            _enforce_relay_response_binding,
        ),
        credential_actions="forbidden",
    )

    def validate_options(self, raw: Mapping[str, Any]) -> RunnerOptions:
        config = raw if hasattr(raw, "runner_options") else None
        supplied = getattr(raw, "runner_options", None) if config is not None else raw
        if not isinstance(supplied, Mapping):
            raise ValueError("relay_runner_options_missing")
        unknown = set(supplied) - _OPTION_KEYS
        if unknown:
            raise ValueError("relay_runner_options_unknown_key")
        if "spool_dir" not in supplied:
            raise ValueError("relay_spool_dir_missing")
        if "relay_protocol_version" not in supplied:
            raise ValueError("relay_protocol_version_missing")
        spool = _validated_directory(Path(str(supplied["spool_dir"])), "relay_spool_dir_invalid")
        _enforce_relay_protocol_version(str(supplied["relay_protocol_version"]))
        poll = supplied.get("poll_seconds", 1.0)
        if isinstance(poll, bool) or not isinstance(poll, (int, float)):
            raise ValueError("relay_poll_seconds_invalid")
        poll = float(poll)
        if not 0.05 <= poll <= 5.0:
            raise ValueError("relay_poll_seconds_invalid")
        children: dict[str, Path] = {}
        child_identities: dict[str, tuple[int, int]] = {}
        for name in ("requests", "responses"):
            child = spool / name
            child.mkdir(exist_ok=True)
            children[name] = _validated_directory(child, "relay_spool_subdirectory_invalid")
            child_identities[name] = _directory_identity(
                children[name], "relay_spool_subdirectory_changed"
            )
        values = MappingProxyType(
            {
                "spool_dir": spool,
                "requests_dir": children["requests"],
                "responses_dir": children["responses"],
                "requests_dir_identity": child_identities["requests"],
                "responses_dir_identity": child_identities["responses"],
                "relay_protocol_version": RELAY_PROTOCOL_VERSION,
                "poll_seconds": poll,
            }
        )
        return RunnerOptions(values=values, config=config)

    def probe(self, options: RunnerOptions) -> RunnerHandle:
        _validated_directory(options.values["spool_dir"], "relay_spool_dir_invalid")
        return RunnerHandle(resource=options.values["spool_dir"], options=options)

    def generate(
        self,
        handle: RunnerHandle,
        request: GenerationRequest,
        stop_event: Any,
        health_check: Callable[[], None] | None,
    ):
        if len(request.prompt.encode("utf-8")) > request.max_input_bytes:
            raise _policy("relay_prompt_exceeds_max_input_bytes")
        if not _SAFE_COMPONENT.fullmatch(request.attempt_id):
            raise _policy("relay_attempt_id_invalid")
        values = handle.options.values
        requests_dir = _validated_directory(values["requests_dir"], "relay_spool_subdirectory_invalid")
        responses_dir = _validated_directory(values["responses_dir"], "relay_spool_subdirectory_invalid")
        nonce = secrets.token_hex(32)
        stem = f"{request.attempt_id}.{request.lease[2]}.{nonce}"
        packet_path = requests_dir / f"{stem}.packet.json"
        response_path = responses_dir / f"{stem}.response.json"
        failure_path = responses_dir / f"{stem}.failed.json"
        temp_path = requests_dir / f".{stem}.{secrets.token_hex(8)}.tmp"
        packet = {
            "protocol": RELAY_PROTOCOL_VERSION,
            "job_id": request.job_id,
            "attempt_id": request.attempt_id,
            "lease_generation": request.lease[2],
            "nonce": nonce,
            "prompt": request.prompt,
            "max_input_bytes": request.max_input_bytes,
            "max_output_bytes": request.max_output_bytes,
            "expected_output": EXPECTED_OUTPUT,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        retain = bool(getattr(handle.options.config, "retain_artifacts", False))
        try:
            with temp_path.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(packet, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
                sync_open_file(stream)
            durable_replace_file(temp_path, packet_path, source_synced=True)
            deadline = time.monotonic() + request.timeout_seconds
            while time.monotonic() < deadline:
                if stop_event.is_set():
                    # This is the controller mirror of the Codex path's normal
                    # watchdog shutdown classification, not a host policy error.
                    raise _infra("watchdog_shutdown")
                if health_check is not None:
                    health_check()
                for candidate, failed in ((failure_path, True), (response_path, False)):
                    document = self._read_document(
                        candidate,
                        responses_dir,
                        values["responses_dir_identity"],
                        request.max_output_bytes,
                    )
                    if document is None:
                        continue
                    _enforce_relay_response_binding(document, request, nonce)
                    expected = {"protocol", "attempt_id", "lease_generation", "nonce"}
                    expected.add("failed" if failed else "output")
                    if set(document) != expected:
                        raise _policy("relay_response_schema_invalid")
                    if failed:
                        code = document.get("failed")
                        if not isinstance(code, str) or not _FAILURE_CODE.fullmatch(code):
                            raise _policy("relay_failure_code_invalid")
                        raise _policy(f"relay_reported_failure:{code}")
                    output = document.get("output")
                    if not isinstance(output, dict):
                        raise _policy("relay_response_output_invalid")
                    reservation = int(
                        getattr(handle.options.config, "max_tokens_per_task", DEFAULT_FULL_RESERVATION_TOKENS)
                    )
                    usage = {key: 0 for key in FULL_RESERVATION_USAGE_KEYS}
                    usage["input_tokens"] = reservation
                    generated = _worker_type("_GeneratedOutput")
                    return generated(output, usage)  # type: ignore[operator]
                stop_event.wait(float(values["poll_seconds"]))
            raise _infra("relay_response_timeout")
        finally:
            if not retain:
                for artifact, directory in (
                    (temp_path, requests_dir),
                    (packet_path, requests_dir),
                    (response_path, responses_dir),
                    (failure_path, responses_dir),
                ):
                    identity = values[
                        "requests_dir_identity"
                        if directory == requests_dir
                        else "responses_dir_identity"
                    ]
                    _safe_unlink(artifact, directory, identity)

    @staticmethod
    def _read_document(
        path: Path,
        pinned_directory: Path,
        identity: tuple[int, int],
        maximum: int,
    ) -> Mapping[str, Any] | None:
        try:
            directory = _revalidate_pinned_directory(
                pinned_directory, identity, "relay_spool_subdirectory_changed"
            )
            if path.parent != directory:
                raise _infra("relay_response_is_link")
            # Distinguish the normal polling state from an unsafe or indeterminate
            # path. A not-yet-published response is expected and must keep waiting;
            # any other lstat failure is indeterminate and therefore fails closed.
            try:
                entry = path.lstat()
            except FileNotFoundError:
                return None
            except OSError:
                raise _infra("relay_response_read_failed") from None
            if stat.S_ISLNK(entry.st_mode):
                raise _infra("relay_response_is_link")
            if os.name == "nt":
                # On Windows a reparse point can only be excluded from the file
                # attributes. An absent or unusable attribute is indeterminate, so
                # it must fail closed rather than be read as "not a link".
                attributes = getattr(entry, "st_file_attributes", None)
                if attributes is None:
                    raise _infra("relay_response_read_failed")
                try:
                    reparse = bool(int(attributes) & _REPARSE_POINT)
                except (TypeError, ValueError):
                    raise _infra("relay_response_read_failed") from None
                if reparse:
                    raise _infra("relay_response_is_link")
            if not stat.S_ISREG(entry.st_mode):
                raise _infra("relay_response_is_link")
            # Identity checks bound fallback races to the interval between the last
            # check and operation. The spool ACL remains the safety boundary; these
            # checks are defence in depth, not a kernel-level guarantee.
            _revalidate_pinned_directory(
                directory, identity, "relay_spool_subdirectory_changed"
            )
            if _supports_handle_relative_io():
                directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                directory_info = os.fstat(directory_fd)
                if (directory_info.st_dev, directory_info.st_ino) != identity:
                    os.close(directory_fd)
                    raise _infra("relay_spool_subdirectory_changed")
                try:
                    descriptor = os.open(
                        path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd
                    )
                except Exception:
                    os.close(directory_fd)
                    raise
                stream = os.fdopen(descriptor, "rb")
            else:
                directory_fd = None
                stream = path.open("rb")
            try:
                with stream:
                    opened = os.fstat(stream.fileno())
                    if opened.st_size > maximum:
                        raise _policy("relay_response_exceeds_max_output_bytes")
                    # Bounded read: the host controls this file and could keep appending
                    # while we read, so never read past the declared limit plus one byte
                    # (which is enough to still detect the oversize case below).
                    raw = stream.read(maximum + 1)
                if directory_fd is not None:
                    current = os.stat(
                        path.name, dir_fd=directory_fd, follow_symlinks=False
                    )
                    directory_info = os.fstat(directory_fd)
                    if (directory_info.st_dev, directory_info.st_ino) != identity:
                        raise _infra("relay_spool_subdirectory_changed")
                else:
                    _revalidate_pinned_directory(
                        directory, identity, "relay_spool_subdirectory_changed"
                    )
                    current = path.stat()
            finally:
                if directory_fd is not None:
                    os.close(directory_fd)
        except FileNotFoundError:
            return None
        except PermissionError:
            raise _infra("relay_response_read_failed") from None
        except OSError:
            raise _infra("relay_response_read_failed") from None
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise _infra("relay_response_identity_changed")
        if len(raw) > maximum:
            raise _policy("relay_response_exceeds_max_output_bytes")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _policy("relay_response_json_invalid") from None
        if not isinstance(document, dict):
            raise _policy("relay_response_schema_invalid")
        return document


HOST_RELAY_ADAPTER = HostRelayAdapter()
