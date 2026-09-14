"""Stdlib-only relay broker. No model, credentials, job writes, or raw diagnostics."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import sqlite3
import stat
import sys
import threading
import time
from datetime import datetime, timezone

PROTOCOL = "vector-lake-ingest-relay/v1"
EXPECTED = "JSON array consumable by finalize_ingest(files_written, processed_data)"
MAX_PACKET = 4 * 1024 * 1024
MAX_INPUT = 196608
MAX_OUTPUT = 1024 * 1024
MAX_SECONDS = 900
MAX_IPC = 8 * 1024 * 1024


class GateError(RuntimeError):
    """Only fixed, non-sensitive codes may cross IPC."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise GateError(code)


def identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def plain(path: Path, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    require(not stat.S_ISLNK(info.st_mode), "relay_path_link")
    require(not getattr(info, "st_file_attributes", 0) & 0x400, "relay_path_reparse")
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
            "relay_path_kind")
    if not directory:
        require(info.st_nlink == 1, "relay_file_hardlink")
    return info


class Pins:
    def __init__(self) -> None:
        self.dirs: dict[Path, tuple[int, int]] = {}

    def add(self, directory: Path) -> None:
        require(directory.is_absolute() and directory == Path(os.path.abspath(directory)), "relay_path_relative")
        for path in reversed((directory, *directory.parents)):
            found = identity(plain(path, True))
            require(path not in self.dirs or self.dirs[path] == found, "relay_directory_changed")
            self.dirs[path] = found

    def check(self) -> None:
        for path, original in self.dirs.items():
            require(identity(plain(path, True)) == original, "relay_directory_changed")

    def read(self, path: Path, limit: int) -> tuple[bytes, tuple[int, int]]:
        self.check()
        before = plain(path)
        require(before.st_size <= limit, "relay_file_oversize")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        try:
            opened = os.fstat(fd)
            require(identity(before) == identity(opened), "relay_file_changed")
            data = bytearray()
            while len(data) <= limit:
                chunk = os.read(fd, min(65536, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            after = plain(path)
            require(identity(after) == identity(opened) and after.st_mtime_ns == before.st_mtime_ns
                    and after.st_size == before.st_size, "relay_file_changed")
            require(len(data) <= limit, "relay_file_oversize")
            self.check()
            return bytes(data), identity(opened)
        finally:
            os.close(fd)


def decode(raw: bytes) -> dict:
    value = json.loads(raw.decode("utf-8"))
    require(isinstance(value, dict), "relay_json_object_required")
    return value


def utc(value: str) -> float:
    require(isinstance(value, str), "relay_time_invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, "relay_time_timezone_required")
    return parsed.astimezone(timezone.utc).timestamp()


class Singleton:
    def __init__(self, path: Path, pins: Pins) -> None:
        pins.add(path.parent)
        if os.path.lexists(path):
            plain(path)
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            self.original = identity(os.fstat(self.fd))
            require(identity(plain(path)) == self.original, "relay_lock_changed")
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.path = path
        except BaseException:
            os.close(self.fd)
            raise

    def check(self) -> None:
        require(identity(plain(self.path)) == self.original, "relay_lock_changed")

    def close(self) -> None:
        os.close(self.fd)


class Broker:
    def __init__(self, manifest: Path, profile: str, lock: Path) -> None:
        self.pins = Pins()
        self.pins.add(manifest.parent)
        raw, file_id = self.pins.read(manifest, 65536)
        document = decode(raw)
        require(document.get("schema_version") == 1, "relay_profile_schema")
        env = document["profiles"][profile]["env"]
        memory = Path(env["VECTOR_LAKE_MEMORY_DIR"]).expanduser()
        self.meta = Path(env["VECTOR_LAKE_META_DIR"]).expanduser()
        require(self.meta.is_relative_to(memory), "relay_meta_outside_memory")
        self.pins.add(self.meta)
        self.config_path = self.meta / "auto_ingest_config.json"
        config_raw, config_id = self.pins.read(self.config_path, 262144)
        self.config = decode(config_raw)
        self.authority_files = ((manifest, file_id, hashlib.sha256(raw).digest()),
                                (self.config_path, config_id, hashlib.sha256(config_raw).digest()))
        self.authority()
        options = self.config["runner_options"]
        require(options["relay_protocol_version"] == PROTOCOL, "relay_protocol_invalid")
        self.spool = Path(options["spool_dir"]).expanduser()
        require(self.spool.is_relative_to(self.meta), "relay_spool_outside_meta")
        self.requests, self.responses = self.spool / "requests", self.spool / "responses"
        self.pins.add(self.requests)
        self.pins.add(self.responses)
        self.database = self.meta / "vector_lake.db"
        self.database_id = identity(plain(self.database))
        self.lock = Singleton(lock, self.pins)
        try:
            self.claims = self.spool / "consumer-claims"
            self.claims.mkdir(exist_ok=True, mode=0o700)
            self.pins.add(self.claims)
        except BaseException:
            self.lock.close()
            raise

    def authority(self) -> None:
        require(self.config.get("schema_version") == 1 and self.config.get("enabled") is True
                and self.config.get("allow_model_processing_raw_text") is True
                and self.config.get("runner") == "host_relay", "relay_consent_required")
        for path, original, digest in self.authority_files:
            raw, current = self.pins.read(path, 262144)
            require(current == original and hashlib.sha256(raw).digest() == digest,
                    "relay_authority_changed")
        state = decode(self.pins.read(self.meta / ".auto_ingest_controller_state.json", 262144)[0])
        until = state.get("circuit_open_until")
        require(not until or utc(until) <= time.time(), "relay_circuit_open")

    def lease(self, packet: dict) -> None:
        self.pins.check()
        self.lock.check()
        self.authority()
        require(identity(plain(self.database)) == self.database_id, "relay_database_changed")
        for suffix in ("-wal", "-shm", "-journal"):
            path = Path(str(self.database) + suffix)
            if os.path.lexists(path):
                plain(path)
        connection = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=1)
        try:
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute(
                "SELECT status, lease_generation, lease_until, completed_at, lease_owner, "
                "json_extract(payload, '$.attempt_id') FROM jobs "
                "WHERE job_id=? AND task_type='ingest' AND json_valid(payload)",
                (packet["job_id"],),
            ).fetchone()
        finally:
            connection.close()
        require(row is not None and row[0] == "subagent_processing"
                and row[1] == packet["lease_generation"] and row[3] is None
                and bool(row[4]) and row[5] == packet["attempt_id"], "relay_lease_invalid")
        require(utc(row[2]) > time.time() + 2, "relay_lease_expired")
        require(identity(plain(self.database)) == self.database_id, "relay_database_changed")
        self.pins.check()

    def packet(self, path: Path) -> dict:
        raw, file_id = self.pins.read(path, MAX_PACKET)
        packet = decode(raw)
        require(packet.get("protocol") == PROTOCOL and packet.get("expected_output") == EXPECTED,
                "relay_packet_protocol")
        for key in ("job_id", "attempt_id"):
            require(isinstance(packet.get(key), str) and bool(re.fullmatch(r"[0-9a-f]{32}", packet[key])),
                    "relay_packet_identity")
        require(isinstance(packet.get("nonce"), str) and bool(re.fullmatch(r"[0-9a-f]{64}", packet["nonce"])),
                "relay_packet_nonce")
        require(type(packet.get("lease_generation")) is int and packet["lease_generation"] > 0,
                "relay_packet_generation")
        stem = f"{packet['attempt_id']}.{packet['lease_generation']}.{packet['nonce']}"
        require(path.name == stem + ".packet.json", "relay_packet_filename")
        require(isinstance(packet.get("prompt"), str), "relay_packet_prompt")
        for key in ("max_input_bytes", "max_output_bytes"):
            require(type(packet.get(key)) is int and packet[key] > 0, "relay_packet_limits")
        require(packet["max_output_bytes"] >= 4096, "relay_packet_output_limit")
        require(len(packet["prompt"].encode("utf-8")) <= min(MAX_INPUT, packet["max_input_bytes"]),
                "relay_input_oversize")
        age = time.time() - utc(packet["created_at"])
        timeout = self.config.get("timeout_seconds", 1200)
        require(type(timeout) is int and 0 <= age < timeout, "relay_packet_expired")
        packet.update(_path=path, _file_id=file_id, _digest=hashlib.sha256(raw).digest(), _stem=stem,
                      _deadline=time.monotonic() + min(MAX_SECONDS, timeout - age))
        self.lease(packet)
        return packet

    def still_valid(self, packet: dict) -> None:
        require(time.monotonic() < packet["_deadline"], "relay_generation_timeout")
        self.lease(packet)
        raw, file_id = self.pins.read(packet["_path"], MAX_PACKET)
        require(file_id == packet["_file_id"] and hashlib.sha256(raw).digest() == packet["_digest"],
                "relay_packet_changed")
        require(not any(os.path.lexists(self.responses / (packet["_stem"] + suffix))
                        for suffix in (".response.json", ".failed.json")), "relay_already_answered")

    def claim(self, packet: dict) -> bool:
        self.still_valid(packet)
        path = self.claims / f"{packet['attempt_id']}.{packet['lease_generation']}.claimed"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False  # At most once even across stop/restart or a changed nonce.
        try:
            os.write(fd, packet["_stem"].encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        self.still_valid(packet)
        return True

    def publish(self, packet: dict, result: dict) -> None:
        require(result.get("stem") == packet["_stem"], "relay_result_binding")
        # The v1 producer rejects extra envelope keys; job_id belongs only in output.
        document = {key: packet[key] for key in ("protocol", "attempt_id", "lease_generation", "nonce")}
        if "failed" in result:
            require(isinstance(result["failed"], str) and bool(re.fullmatch(r"relay_[a-z0-9_]{1,50}", result["failed"])),
                    "relay_failure_code")
            document["failed"] = result["failed"]
            suffix = ".failed.json"
        else:
            output = result.get("output")
            require(isinstance(output, dict) and output.get("job_id") == packet["job_id"], "relay_output_binding")
            document["output"] = output
            suffix = ".response.json"
        raw = json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8")
        require(len(raw) <= min(MAX_OUTPUT, packet["max_output_bytes"]), "relay_output_oversize")
        self.still_valid(packet)
        temporary = self.responses / (packet["_stem"] + ".consumer.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        try:
            temporary_id = identity(os.fstat(fd))
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                require(written > 0, "relay_short_write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        # Hard-link publication is atomic and refuses an existing target on NTFS/POSIX.
        # Leave temporary files on failure: never guess a safe cleanup target.
        self.still_valid(packet)
        require(identity(plain(temporary)) == temporary_id, "relay_temporary_changed")
        target = self.responses / (packet["_stem"] + suffix)
        os.link(temporary, target, follow_symlinks=False)
        self.pins.check()
        require(identity(temporary.lstat()) == temporary_id and identity(target.lstat()) == temporary_id,
                "relay_temporary_changed")
        os.unlink(temporary)


def emit(value: dict) -> None:
    line = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    require(len(line) <= MAX_IPC, "relay_ipc_oversize")
    sys.stdout.buffer.write(line)
    sys.stdout.buffer.flush()


def reader(inbox: queue.Queue, ended: threading.Event) -> None:
    try:
        while not ended.is_set():
            line = sys.stdin.buffer.readline(MAX_IPC + 1)
            if not line or len(line) > MAX_IPC or not line.endswith(b"\n"):
                ended.set()
                return
            inbox.put(decode(line), timeout=1)
    except (ValueError, UnicodeError, queue.Full, OSError, GateError):
        ended.set()


def serve(broker: Broker) -> None:
    inbox: queue.Queue = queue.Queue(maxsize=4)
    ended = threading.Event()
    threading.Thread(target=reader, args=(inbox, ended), daemon=True).start()
    emit({"type": "status", "code": "relay_ready"})
    seen: set[str] = set()
    while not ended.is_set():
        broker.authority()
        broker.pins.check()
        broker.lock.check()
        count = 0
        for path in broker.requests.iterdir():
            count += 1
            require(count <= 4096 and len(seen) <= 4096, "relay_scan_limit")
            if ended.is_set():
                return
            if not path.name.endswith(".packet.json") or path.name in seen:
                continue
            seen.add(path.name)
            try:
                packet = broker.packet(path)
                if not broker.claim(packet):
                    continue
            except GateError as error:
                emit({"type": "status", "code": str(error)})
                continue
            emit({"type": "generate", "stem": packet["_stem"], "jobId": packet["job_id"],
                  "prompt": packet["prompt"], "maxOutputBytes": min(MAX_OUTPUT, packet["max_output_bytes"]) - 512,
                  "timeoutMs": max(1, int((packet["_deadline"] - time.monotonic()) * 1000))})
            cancelled = False
            while not ended.is_set():
                try:
                    broker.still_valid(packet)
                except GateError:
                    if not cancelled:
                        emit({"type": "cancel", "stem": packet["_stem"]})
                    cancelled = True
                try:
                    result = inbox.get(timeout=0.25)
                except queue.Empty:
                    continue
                require(result.get("stem") == packet["_stem"], "relay_result_binding")
                if not cancelled:
                    broker.publish(packet, result)
                    emit({"type": "status", "code": "relay_published"})
                break
            if cancelled:
                return  # Do not advance after lease loss, timeout or revoked authority.
        ended.wait(1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-profile")
    parser.add_argument("--lock-only", action="store_true")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--lock", required=True)
    args = parser.parse_args()
    broker = None
    try:
        if args.lock_only:
            lock = Singleton(Path(args.lock), Pins())
            try:
                emit({"type": "status", "code": "relay_ready"})
                sys.stdin.buffer.read(1)
            finally:
                lock.close()
            return 0
        require(bool(args.runtime_profile), "relay_profile_required")
        broker = Broker(Path(args.runtime_profile), args.profile, Path(args.lock))
        serve(broker)
        return 0
    except GateError as error:
        emit({"type": "status", "code": str(error)})
        return 2
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        emit({"type": "status", "code": "relay_broker_io_or_schema_error"})
        return 2
    finally:
        if broker is not None:
            broker.lock.close()


if __name__ == "__main__":
    sys.exit(main())
