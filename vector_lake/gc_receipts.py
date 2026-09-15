"""_GC_BACKUP_NAME _GC_RECEIPT_CONTRACT _GC_RECEIPT_NAME _ORPHAN_FINGERPRINT_PREFIX _hash_file _plain_path_stat _read_stable_utf8_file _stable_stat_identity verify_gc_recovery_receipts

Split out of tool_gc so that a lower layer can use it without importing a
handler. The cluster is closed under intra-module references; tool_gc
re-imports these names for its remaining code.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat as stat_module
from pathlib import Path
from vector_lake.wiki_utils import get_wiki_dir, peek_meta_dir


_ORPHAN_FINGERPRINT_PREFIX = "sha256:"

_GC_RECEIPT_CONTRACT = "vector-lake-orphan-gc-receipt-v1"

_GC_RECEIPT_NAME = re.compile(r"^[0-9a-f]{64}\.json$")

_GC_BACKUP_NAME = re.compile(r"^[0-9a-f]{16}$")

def _plain_path_stat(path, *, directory: bool):
    details = path.lstat()
    file_attributes = getattr(details, "st_file_attributes", 0)
    reparse_attribute = getattr(
        stat_module,
        "FILE_ATTRIBUTE_REPARSE_POINT",
        0,
    )
    if stat_module.S_ISLNK(details.st_mode) or (
        reparse_attribute and file_attributes & reparse_attribute
    ):
        raise RuntimeError(f"GC path is a symbolic link or reparse point: {path}")
    expected = (
        stat_module.S_ISDIR(details.st_mode)
        if directory
        else stat_module.S_ISREG(details.st_mode)
    )
    if not expected:
        kind = "directory" if directory else "regular file"
        raise RuntimeError(f"GC path is not a plain {kind}: {path}")
    return details

def _stable_stat_identity(details) -> tuple[int, int, int, int, int]:
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_mode),
        int(details.st_size),
        int(details.st_mtime_ns),
    )

def _read_stable_utf8_file(path, *, max_bytes: int) -> str:
    before = _plain_path_stat(path, directory=False)
    if int(before.st_size) > max_bytes:
        raise RuntimeError(f"GC file is unexpectedly large: {path}")
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if _stable_stat_identity(opened) != _stable_stat_identity(before):
            raise RuntimeError(f"GC file changed before reading: {path}")
        raw = handle.read(max_bytes + 1)
        after = os.fstat(handle.fileno())
    current = _plain_path_stat(path, directory=False)
    if len(raw) > max_bytes:
        raise RuntimeError(f"GC file is unexpectedly large: {path}")
    if _stable_stat_identity(after) != _stable_stat_identity(
        opened
    ) or _stable_stat_identity(current) != _stable_stat_identity(after):
        raise RuntimeError(f"GC file changed while reading: {path}")
    return raw.decode("utf-8")

def _hash_file(path) -> str:
    before = _plain_path_stat(path, directory=False)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if _stable_stat_identity(opened) != _stable_stat_identity(before):
            raise RuntimeError(f"GC file changed before hashing: {path}")
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    current = _plain_path_stat(path, directory=False)
    if _stable_stat_identity(after) != _stable_stat_identity(
        opened
    ) or _stable_stat_identity(current) != _stable_stat_identity(after):
        raise RuntimeError(f"GC file changed while hashing: {path}")
    return digest.hexdigest()

def verify_gc_recovery_receipts(*, deep: bool = False) -> dict:
    """Validate durable GC receipts without creating runtime directories."""
    root = peek_meta_dir() / "gc-runs"
    summary = {
        "receipt_root": str(root),
        "receipts": 0,
        "committed": 0,
        "pending": 0,
        "aborted": 0,
        "issues": [],
        "warnings": [],
    }
    if not root.exists():
        return summary
    try:
        _plain_path_stat(root, directory=True)
        entries = sorted(root.iterdir(), key=lambda path: path.name)
    except (OSError, RuntimeError) as exc:
        summary["issues"].append(f"gc_receipt_root_invalid:{type(exc).__name__}")
        return summary
    if len(entries) > 1_000:
        summary["issues"].append(f"gc_receipt_scan_limit_exceeded:{len(entries)}")
        return summary

    backup_root = get_wiki_dir().parent / "backup" / "gc"
    for receipt_path in entries:
        if not _GC_RECEIPT_NAME.fullmatch(receipt_path.name):
            summary["issues"].append(f"gc_receipt_name_invalid:{receipt_path.name}")
            continue
        digest = receipt_path.stem
        try:
            _plain_path_stat(receipt_path, directory=False)
            receipt = json.loads(
                _read_stable_utf8_file(receipt_path, max_bytes=2_000_000)
            )
            fingerprint = str(receipt.get("fingerprint") or "")
            if (
                receipt.get("contract") != _GC_RECEIPT_CONTRACT
                or fingerprint != _ORPHAN_FINGERPRINT_PREFIX + digest
            ):
                raise RuntimeError("receipt contract or fingerprint mismatch")
            status = str(receipt.get("status") or "")
            status_bucket = None
            if status == "committed":
                status_bucket = "committed"
            elif status == "aborted":
                status_bucket = "aborted"
            elif status in {"prepared", "commit_pending"}:
                status_bucket = "pending"
                summary["issues"].append(f"gc_receipt_incomplete:{digest}")
            else:
                raise RuntimeError("unsupported receipt status")

            backup_name = str(receipt.get("backup_name") or "")
            if not _GC_BACKUP_NAME.fullmatch(backup_name):
                raise RuntimeError("backup name is invalid")
            backup_dir = backup_root / backup_name
            _plain_path_stat(backup_dir, directory=True)
            manifest_path = backup_dir / "manifest.json"
            _plain_path_stat(manifest_path, directory=False)
            expected_manifest_hash = str(
                receipt.get("backup_manifest_sha256") or ""
            )
            if not hmac.compare_digest(
                _hash_file(manifest_path), expected_manifest_hash
            ):
                raise RuntimeError("backup manifest hash mismatch")
            manifest = json.loads(
                _read_stable_utf8_file(manifest_path, max_bytes=1_000_000)
            )
            files = manifest.get("files")
            if (
                manifest.get("kind") != "vector-lake-orphan-gc"
                or manifest.get("fingerprint") != fingerprint
                or not isinstance(files, list)
                or int(receipt.get("candidate_count") or -1) != len(files)
                or receipt.get("candidate_files") != files
            ):
                raise RuntimeError("backup manifest content mismatch")
            if deep:
                expected_names = {"manifest.json"}
                for record in files:
                    filename = str(record.get("filename") or "")
                    if not filename or Path(filename).name != filename:
                        raise RuntimeError("backup filename is invalid")
                    expected_names.add(filename)
                    backup_file = backup_dir / filename
                    file_stat = _plain_path_stat(backup_file, directory=False)
                    if int(file_stat.st_size) != int(record.get("size") or -1):
                        raise RuntimeError("backup file size mismatch")
                    if not hmac.compare_digest(
                        _hash_file(backup_file),
                        str(record.get("content_sha256") or ""),
                    ):
                        raise RuntimeError("backup file hash mismatch")
                if {path.name for path in backup_dir.iterdir()} != expected_names:
                    raise RuntimeError("backup file set mismatch")
            summary["receipts"] += 1
            summary[status_bucket] += 1
        except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            summary["issues"].append(
                f"gc_receipt_invalid:{digest}:{type(exc).__name__}:{exc}"
            )
    return summary
