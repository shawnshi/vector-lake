"""Single source of truth for raw-file revision identities."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterable


_LEGACY_MD5_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_CANONICAL_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_READ_CHUNK_BYTES = 1024 * 1024


class RawRevisionFormatError(ValueError):
    """A stored revision is neither exact legacy MD5 nor canonical SHA-256."""


class RawSourceUnstableError(RuntimeError):
    """The path or its open handle changed during a revision read."""


class RawSourceTooLargeError(ValueError):
    """The stable raw read exceeded its caller-owned byte limit."""


class RawSourceContainmentError(ValueError):
    """The lexical raw path escaped a root or traversed a reparse point."""


def parse_revision(value: object) -> tuple[str, str]:
    """Parse one exact supported revision without case or prefix coercion."""
    if not isinstance(value, str):
        raise RawRevisionFormatError("raw_revision_must_be_a_string")
    if _LEGACY_MD5_PATTERN.fullmatch(value):
        return "md5", value
    if _CANONICAL_SHA256_PATTERN.fullmatch(value):
        return "sha256", value[7:]
    raise RawRevisionFormatError("raw_revision_format_is_unsupported")


def is_supported_revision(value: object) -> bool:
    try:
        parse_revision(value)
    except RawRevisionFormatError:
        return False
    return True


def is_canonical_revision(value: object) -> bool:
    return isinstance(value, str) and _CANONICAL_SHA256_PATTERN.fullmatch(value) is not None


def _handle_identity(details: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_mode),
        int(details.st_size),
        int(details.st_mtime_ns),
    )


def _path_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        *_handle_identity(details),
        int(details.st_ctime_ns),
    )


def _path_is_reparse_point(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(
            getattr(path, "is_junction", lambda: False)()
        )
    except OSError:
        return True


def _contained_lexical_path(
    filepath: str | os.PathLike[str],
    allowed_roots: Iterable[str | os.PathLike[str]],
) -> Path:
    lexical = Path(os.path.abspath(os.fspath(filepath)))
    for raw_root in allowed_roots:
        root = Path(os.path.abspath(os.fspath(raw_root)))
        try:
            relative = lexical.relative_to(root)
        except ValueError:
            continue
        current = root
        if _path_is_reparse_point(current):
            raise RawSourceContainmentError("raw_source_root_is_a_reparse_point")
        for part in relative.parts:
            current = current / part
            if _path_is_reparse_point(current):
                raise RawSourceContainmentError(
                    "raw_source_path_contains_reparse_point"
                )
        resolved_root = root.resolve(strict=True)
        resolved_path = lexical.resolve(strict=True)
        if resolved_path != resolved_root and not resolved_path.is_relative_to(
            resolved_root
        ):
            raise RawSourceContainmentError("raw_source_resolved_outside_root")
        return lexical
    raise RawSourceContainmentError("raw_source_lexically_outside_allowed_roots")


@dataclass(frozen=True)
class StableRawMetadata:
    path: Path
    observed_mtime_ns: int
    observed_size: int
    stat_identity: tuple[int, int, int, int, int, int]


@dataclass(frozen=True)
class StableRawRevision:
    path: Path
    canonical_revision: str
    legacy_md5: str | None
    observed_mtime_ns: int
    observed_size: int
    stat_identity: tuple[int, int, int, int, int, int]
    data: bytes | None = field(default=None, repr=False)

    def matches(self, revision: object) -> bool:
        kind, digest = parse_revision(revision)
        if kind == "md5":
            if self.legacy_md5 is None:
                return False
            actual = self.legacy_md5
        else:
            actual = self.canonical_revision[7:]
        return hmac.compare_digest(actual, digest)


def stable_raw_metadata(
    filepath: str | os.PathLike[str],
    *,
    allowed_roots: Iterable[str | os.PathLike[str]] | None = None,
) -> StableRawMetadata:
    """Capture stable path metadata without reading raw-file contents."""
    lexical = (
        _contained_lexical_path(filepath, allowed_roots)
        if allowed_roots is not None
        else Path(os.path.abspath(os.fspath(filepath)))
    )
    path = lexical.resolve(strict=True)
    before_path = path.stat()
    if not stat.S_ISREG(before_path.st_mode):
        raise OSError(f"raw_source_is_not_a_regular_file:{path}")
    after_path = path.stat()
    if _path_identity(before_path) != _path_identity(after_path):
        raise RawSourceUnstableError("raw_source_changed_during_metadata_read")
    return StableRawMetadata(
        path=path,
        observed_mtime_ns=int(after_path.st_mtime_ns),
        observed_size=int(after_path.st_size),
        stat_identity=_path_identity(after_path),
    )


def _read_raw_chunk(handle) -> bytes:
    """Keep raw byte reads observable in tests without changing file semantics."""
    return handle.read(_READ_CHUNK_BYTES)


def _open_stable_raw_handle(path: Path) -> BinaryIO:
    """Open one raw file while denying concurrent writes/deletes on Windows."""
    if os.name != "nt":
        return path.open("rb")

    import ctypes
    import msvcrt
    from ctypes import wintypes

    generic_read = 0x80000000
    file_share_read = 0x00000001
    open_existing = 3
    sequential_scan = 0x08000000
    invalid_handle = ctypes.c_void_p(-1).value
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    def windows_opener(_raw_path: str, _flags: int) -> int:
        native_handle = create_file(
            str(path),
            generic_read,
            file_share_read,
            None,
            open_existing,
            sequential_scan,
            None,
        )
        if native_handle in (None, 0, invalid_handle):
            error_code = ctypes.get_last_error()
            if error_code in {32, 33}:
                raise RawSourceUnstableError("raw_source_has_active_writer")
            raise ctypes.WinError(error_code)
        try:
            return msvcrt.open_osfhandle(
                int(native_handle),
                os.O_RDONLY
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOINHERIT", 0),
            )
        except BaseException:
            close_handle(native_handle)
            raise

    return open(path, "rb", opener=windows_opener)


def stable_raw_revision(
    filepath: str | os.PathLike[str],
    *,
    capture_bytes: bool = False,
    max_bytes: int | None = None,
    allowed_roots: Iterable[str | os.PathLike[str]] | None = None,
    include_legacy_md5: bool = True,
) -> StableRawRevision:
    """Hash the file behind one stable handle and verify stat-read-stat identity."""
    lexical = (
        _contained_lexical_path(filepath, allowed_roots)
        if allowed_roots is not None
        else Path(os.path.abspath(os.fspath(filepath)))
    )
    path = lexical.resolve(strict=True)
    before_path = path.stat()
    if not stat.S_ISREG(before_path.st_mode):
        raise OSError(f"raw_source_is_not_a_regular_file:{path}")
    if max_bytes is not None and before_path.st_size > int(max_bytes):
        raise RawSourceTooLargeError("raw_source_exceeds_byte_limit")

    sha256 = hashlib.sha256()
    md5 = None
    if include_legacy_md5:
        try:
            md5 = hashlib.md5(usedforsecurity=False)
        except TypeError:  # pragma: no cover - compatibility for older Python builds
            md5 = hashlib.md5()
    captured = bytearray() if capture_bytes else None

    with _open_stable_raw_handle(path) as handle:
        before_handle = os.fstat(handle.fileno())
        if _handle_identity(before_handle) != _handle_identity(before_path):
            raise RawSourceUnstableError("raw_source_changed_before_read")
        total = 0
        while True:
            chunk = _read_raw_chunk(handle)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > int(max_bytes):
                raise RawSourceTooLargeError("raw_source_exceeds_byte_limit")
            sha256.update(chunk)
            if md5 is not None:
                md5.update(chunk)
            if captured is not None:
                captured.extend(chunk)
        after_handle = os.fstat(handle.fileno())
        after_path = path.stat()
    handle_identities = {
        _handle_identity(before_path),
        _handle_identity(before_handle),
        _handle_identity(after_handle),
        _handle_identity(after_path),
    }
    if len(handle_identities) != 1 or _path_identity(
        before_path
    ) != _path_identity(after_path):
        raise RawSourceUnstableError("raw_source_changed_during_read")
    if total != int(after_path.st_size):
        raise RawSourceUnstableError("raw_source_size_changed_during_read")

    return StableRawRevision(
        path=path,
        canonical_revision=f"sha256:{sha256.hexdigest()}",
        legacy_md5=md5.hexdigest() if md5 is not None else None,
        observed_mtime_ns=int(after_path.st_mtime_ns),
        observed_size=int(after_path.st_size),
        stat_identity=_path_identity(after_path),
        data=bytes(captured) if captured is not None else None,
    )


def snapshot_still_current(snapshot: StableRawRevision) -> bool:
    """Check that the path still resolves to the exact file snapshot identity."""
    try:
        return _path_identity(snapshot.path.stat()) == snapshot.stat_identity
    except OSError:
        return False


def current_file_proves_revisions(
    filepath: str | os.PathLike[str],
    job_revision: object,
    marker_revision: object,
    *,
    allowed_roots: Iterable[str | os.PathLike[str]] | None = None,
) -> bool:
    """Prove a job revision and canonical marker describe the same current bytes."""
    if not is_canonical_revision(job_revision) or not is_canonical_revision(
        marker_revision
    ):
        return False
    try:
        snapshot = stable_raw_revision(
            filepath,
            allowed_roots=allowed_roots,
            include_legacy_md5=False,
        )
        return snapshot.matches(job_revision) and snapshot.matches(marker_revision)
    except (
        OSError,
        RawRevisionFormatError,
        RawSourceContainmentError,
        RawSourceUnstableError,
    ):
        return False
