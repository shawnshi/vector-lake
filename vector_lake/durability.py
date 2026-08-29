"""Cross-platform durability barriers for acknowledged filesystem writes."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
from typing import BinaryIO, TextIO


_DURABILITY_PROFILE_ENV = "VECTOR_LAKE_DURABILITY_PROFILE"
_FULL_PROFILE = "full"
_BEST_EFFORT_PROFILE = "best_effort"


def durability_profile() -> str:
    value = str(os.environ.get(_DURABILITY_PROFILE_ENV, _FULL_PROFILE)).strip().lower()
    if value not in {_FULL_PROFILE, _BEST_EFFORT_PROFILE}:
        raise RuntimeError(
            "VECTOR_LAKE_DURABILITY_PROFILE must be 'full' or 'best_effort'"
        )
    return value


def full_durability_enabled() -> bool:
    return durability_profile() == _FULL_PROFILE


def sync_open_file(handle: BinaryIO | TextIO) -> None:
    """Flush Python and OS buffers for an already-open writable file."""
    if not full_durability_enabled():
        return
    handle.flush()
    os.fsync(handle.fileno())


def sync_file(path: str | os.PathLike[str]) -> None:
    if not full_durability_enabled():
        return
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise RuntimeError(f"durability_file_invalid:{target}")
    with target.open("r+b") as handle:
        os.fsync(handle.fileno())


def _windows_sync_directory(path: Path) -> None:
    generic_write = 0x40000000
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    backup_semantics = 0x02000000
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
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = (wintypes.HANDLE,)
    flush_file_buffers.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        str(path),
        generic_write,
        share_all,
        None,
        open_existing,
        backup_semantics,
        None,
    )
    if handle in (None, 0, invalid_handle):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not flush_file_buffers(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close_handle(handle)


def sync_directory(path: str | os.PathLike[str]) -> None:
    if not full_durability_enabled():
        return
    target = Path(path)
    if target.is_symlink() or not target.is_dir():
        raise RuntimeError(f"durability_directory_invalid:{target}")
    if os.name == "nt":
        _windows_sync_directory(target)
        return
    descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _windows_replace_file_write_through(source: Path, target: Path) -> None:
    movefile_replace_existing = 0x00000001
    movefile_write_through = 0x00000008
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file_ex = kernel32.MoveFileExW
    move_file_ex.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file_ex.restype = wintypes.BOOL
    if not move_file_ex(
        str(source),
        str(target),
        movefile_replace_existing | movefile_write_through,
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def durable_replace_file(
    source: str | os.PathLike[str],
    target: str | os.PathLike[str],
    *,
    source_synced: bool = False,
) -> None:
    """Atomically replace a same-directory file and durably publish its name."""
    source_path = Path(source)
    target_path = Path(target)
    if source_path.is_symlink() or not source_path.is_file():
        raise RuntimeError(f"durability_source_invalid:{source_path}")
    if target_path.is_symlink():
        raise RuntimeError(f"durability_target_is_symlink:{target_path}")
    source_parent = os.path.normcase(os.path.abspath(source_path.parent))
    target_parent = os.path.normcase(os.path.abspath(target_path.parent))
    if source_parent != target_parent:
        raise RuntimeError("durability_replace_requires_same_directory")

    if not full_durability_enabled():
        os.replace(source_path, target_path)
        return
    if not source_synced:
        sync_file(source_path)
    if os.name == "nt":
        _windows_replace_file_write_through(source_path, target_path)
    else:
        os.replace(source_path, target_path)
    sync_file(target_path)
    sync_directory(target_path.parent)


def commit_existing_file(path: str | os.PathLike[str]) -> None:
    """Publish durability after a specialized CAS primitive changed a file."""
    if not full_durability_enabled():
        return
    target = Path(path)
    sync_file(target)
    sync_directory(target.parent)


__all__ = [
    "commit_existing_file",
    "durability_profile",
    "durable_replace_file",
    "full_durability_enabled",
    "sync_directory",
    "sync_file",
    "sync_open_file",
]
