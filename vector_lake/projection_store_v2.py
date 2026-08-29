"""Immutable content-addressed projection objects.

The store is deliberately independent from the mutable projection pointer.  A
successful :meth:`ProjectionStoreV2.apply` returns a new root digest; callers
publish that digest separately only after every referenced object is durable.
An interrupted apply can therefore leave harmless unreachable objects, but it
cannot modify or invalidate the previously published root.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
from typing import Any, Iterable, Iterator, Mapping

from vector_lake import durability


FORMAT_VERSION = 2
MAX_LEAF_ENTRIES = 256
MAX_LEAF_BYTES = 256 * 1024
MAX_OBJECT_BYTES = 1024 * 1024
MAX_DEPTH = 32
MAX_BATCH_MUTATIONS = 1_000_000
MAX_ITER_LIMIT = 1_000_000
MAX_DIFF_LIMIT = 1_000_000
DEFAULT_READ_OBJECT_LIMIT = 8_192

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_EMPTY_LEAF = {"entries": [], "kind": "leaf", "version": FORMAT_VERSION}


class ProjectionStoreError(RuntimeError):
    """Base error for projection-store failures."""


class ProjectionIntegrityError(ProjectionStoreError):
    """Stored content or a caller-supplied root failed validation."""


class ProjectionSecurityError(ProjectionStoreError):
    """A path escaped the store or crossed a redirecting filesystem object."""


class ProjectionObjectLimitError(ProjectionStoreError):
    """A configured object, batch, or read bound was exceeded."""


class ProjectionDepthError(ProjectionObjectLimitError):
    """A trie would need to exceed the maximum persisted depth."""


def canonical_json_bytes(value: Any) -> bytes:
    """Return the sole accepted JSON representation for persisted objects."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProjectionIntegrityError(f"canonical_json_invalid:{exc}") from exc


def key_digest(key: str) -> str:
    if not isinstance(key, str):
        raise ProjectionIntegrityError("key_must_be_string")
    try:
        payload = key.encode("utf-8")
    except UnicodeError as exc:
        raise ProjectionIntegrityError("key_utf8_invalid") from exc
    return hashlib.sha256(payload).hexdigest()


EMPTY_ROOT_DIGEST = hashlib.sha256(canonical_json_bytes(_EMPTY_LEAF)).hexdigest()


@dataclass(frozen=True)
class MutationResult:
    root_digest: str
    new_objects: int
    reused_objects: int
    new_bytes: int
    reused_bytes: int


@dataclass(frozen=True)
class DiffEntry:
    key: str
    left_exists: bool
    left: Any
    right_exists: bool
    right: Any


@dataclass(frozen=True)
class DiffResult:
    entries: tuple[DiffEntry, ...]
    truncated: bool


@dataclass
class _WriteStats:
    new_objects: int = 0
    reused_objects: int = 0
    new_bytes: int = 0
    reused_bytes: int = 0
    new_digests: set[str] = field(default_factory=set)
    reused_digests: set[str] = field(default_factory=set)
    secure_directories: dict[str, tuple[int, int]] = field(default_factory=dict)

    def record_new(self, digest: str, size: int) -> None:
        if digest in self.new_digests:
            return
        self.new_digests.add(digest)
        self.new_objects += 1
        self.new_bytes += size

    def record_reused(self, digest: str, size: int) -> None:
        if digest in self.new_digests or digest in self.reused_digests:
            return
        self.reused_digests.add(digest)
        self.reused_objects += 1
        self.reused_bytes += size


@dataclass
class _ReadContext:
    max_objects: int
    objects_read: int = 0
    raw_cache: dict[str, tuple[dict[str, Any], int]] = field(default_factory=dict)
    secure_directories: dict[str, tuple[int, int]] = field(default_factory=dict)
    secure_directory_paths: dict[str, Path] = field(default_factory=dict)
    defer_directory_rechecks: bool = False


def _file_attributes(path_stat: os.stat_result) -> int:
    return int(getattr(path_stat, "st_file_attributes", 0) or 0)


def _is_reparse(path_stat: os.stat_result) -> bool:
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(_file_attributes(path_stat) & marker)


def _normal_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _decode_json_strict(payload: bytes) -> Any:
    def no_duplicates(pairs):
        decoded = {}
        for key, value in pairs:
            if key in decoded:
                raise ProjectionIntegrityError(f"duplicate_json_key:{key}")
            decoded[key] = value
        return decoded

    try:
        text = payload.decode("utf-8")
        return json.loads(text, object_pairs_hook=no_duplicates)
    except ProjectionIntegrityError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectionIntegrityError(f"json_invalid:{exc}") from exc


def _entry_order(key: str) -> tuple[str, str]:
    return key_digest(key), key


class ProjectionStoreV2:
    """Persistent branch-16 hash trie backed by immutable JSON objects."""

    def __init__(self, base_dir: str | os.PathLike[str]) -> None:
        raw_base = Path(base_dir)
        if any(part == ".." for part in raw_base.parts):
            raise ProjectionSecurityError("base_path_traversal")
        self.base_dir = Path(os.path.abspath(os.fspath(raw_base)))
        self.store_dir = self.base_dir / ".projection-store"
        self.objects_dir = self.store_dir / "objects" / "sha256"

    @property
    def empty_root_digest(self) -> str:
        return EMPTY_ROOT_DIGEST

    def object_path(self, digest: str) -> Path:
        self._validate_digest(digest)
        # A validated digest is exactly 64 lowercase hex characters, so the
        # two derived path segments cannot contain traversal syntax.  Avoid a
        # redundant common-path normalization on every cold object read.
        return self.objects_dir / digest[:2] / f"{digest}.json"

    def apply(
        self,
        root_digest: str | None,
        *,
        sets: Mapping[str, Any] | None = None,
        deletes: Iterable[str] = (),
    ) -> MutationResult:
        root = EMPTY_ROOT_DIGEST if root_digest is None else root_digest
        self._validate_digest(root)
        normalized_sets = self._normalize_sets(sets or {})
        normalized_deletes = self._normalize_deletes(deletes)
        overlap = set(normalized_sets).intersection(normalized_deletes)
        if overlap:
            raise ProjectionIntegrityError(
                f"set_delete_overlap:{min(overlap, key=_entry_order)}"
            )
        if len(normalized_sets) + len(normalized_deletes) > MAX_BATCH_MUTATIONS:
            raise ProjectionObjectLimitError("batch_mutation_limit")

        reads = _ReadContext(max_objects=DEFAULT_READ_OBJECT_LIMIT)
        writes = _WriteStats()
        new_root = self._mutate_subtree(
            root,
            depth=0,
            prefix="",
            sets=normalized_sets,
            deletes=normalized_deletes,
            reads=reads,
            writes=writes,
        )
        return MutationResult(
            root_digest=new_root,
            new_objects=writes.new_objects,
            reused_objects=writes.reused_objects,
            new_bytes=writes.new_bytes,
            reused_bytes=writes.reused_bytes,
        )

    def get(self, root_digest: str, key: str, default: Any = ...) -> Any:
        self._validate_digest(root_digest)
        self._validate_key(key)
        reads = _ReadContext(max_objects=MAX_DEPTH + 1)
        digest = root_digest
        depth = 0
        prefix = ""
        while True:
            node, _size = self._load_node(digest, depth, prefix, reads)
            if node["kind"] == "leaf":
                for stored_key, value in node["entries"]:
                    if stored_key == key:
                        return value
                if default is ...:
                    raise KeyError(key)
                return default
            slot = int(key_digest(key)[depth], 16)
            child = dict(node["children"]).get(slot)
            if child is None:
                if default is ...:
                    raise KeyError(key)
                return default
            digest = child
            prefix += format(slot, "x")
            depth += 1

    def iter_items(
        self,
        root_digest: str,
        *,
        limit: int,
        start_after: str | None = None,
        max_objects: int = DEFAULT_READ_OBJECT_LIMIT,
    ) -> tuple[tuple[str, Any], ...]:
        self._validate_digest(root_digest)
        self._validate_read_bounds(limit, max_objects, MAX_ITER_LIMIT)
        cursor = None
        if start_after is not None:
            self._validate_key(start_after)
            cursor = _entry_order(start_after)
        # A full iteration can touch thousands of immutable objects while only
        # visiting a few hundred content-address shards.  Validate every shard
        # before its first object and verify its identity again at the batch
        # boundary; every object still receives its own lstat/open/fstat/hash
        # validation.  This avoids repeating the same directory lstat for each
        # object without trusting a directory beyond the completed iteration.
        reads = _ReadContext(
            max_objects=max_objects,
            defer_directory_rechecks=True,
        )
        result: list[tuple[str, Any]] = []
        try:
            for key, value in self._iter_subtree(root_digest, 0, "", reads):
                if cursor is not None and _entry_order(key) <= cursor:
                    continue
                if len(result) >= limit:
                    break
                result.append((key, value))
            return tuple(result)
        finally:
            self._verify_secure_read_directories(reads)

    def diff(
        self,
        left_root: str,
        right_root: str,
        *,
        limit: int,
        max_objects: int = DEFAULT_READ_OBJECT_LIMIT,
    ) -> DiffResult:
        self._validate_digest(left_root)
        self._validate_digest(right_root)
        self._validate_read_bounds(limit, max_objects, MAX_DIFF_LIMIT)
        if left_root == right_root:
            reads = _ReadContext(max_objects=max_objects)
            self._load_node(left_root, 0, "", reads)
            return DiffResult(entries=(), truncated=False)

        left_reads = _ReadContext(max_objects=max_objects)
        right_reads = _ReadContext(max_objects=max_objects)
        left = iter(self._iter_subtree(left_root, 0, "", left_reads))
        right = iter(self._iter_subtree(right_root, 0, "", right_reads))
        sentinel = object()
        left_item: Any = next(left, sentinel)
        right_item: Any = next(right, sentinel)
        differences: list[DiffEntry] = []
        while left_item is not sentinel or right_item is not sentinel:
            if left_item is sentinel:
                key, value = right_item
                difference = DiffEntry(key, False, None, True, value)
                right_item = next(right, sentinel)
            elif right_item is sentinel:
                key, value = left_item
                difference = DiffEntry(key, True, value, False, None)
                left_item = next(left, sentinel)
            else:
                left_order = _entry_order(left_item[0])
                right_order = _entry_order(right_item[0])
                if left_order < right_order:
                    key, value = left_item
                    difference = DiffEntry(key, True, value, False, None)
                    left_item = next(left, sentinel)
                elif right_order < left_order:
                    key, value = right_item
                    difference = DiffEntry(key, False, None, True, value)
                    right_item = next(right, sentinel)
                else:
                    left_key, left_value = left_item
                    _right_key, right_value = right_item
                    left_item = next(left, sentinel)
                    right_item = next(right, sentinel)
                    if canonical_json_bytes(left_value) == canonical_json_bytes(
                        right_value
                    ):
                        continue
                    difference = DiffEntry(
                        left_key,
                        True,
                        left_value,
                        True,
                        right_value,
                    )
            if len(differences) == limit:
                return DiffResult(entries=tuple(differences), truncated=True)
            differences.append(difference)
        return DiffResult(entries=tuple(differences), truncated=False)

    def object_paths(
        self,
        root_digest: str,
        *,
        max_objects: int,
    ) -> tuple[Path, ...]:
        self._validate_digest(root_digest)
        self._validate_read_bounds(max_objects, max_objects, max_objects)
        reads = _ReadContext(max_objects=max_objects)
        result: list[Path] = []
        pending: list[tuple[str, int, str]] = [(root_digest, 0, "")]
        seen: set[str] = set()
        while pending:
            digest, depth, prefix = pending.pop()
            if digest in seen:
                continue
            node, _size = self._load_node(digest, depth, prefix, reads)
            seen.add(digest)
            if digest != EMPTY_ROOT_DIGEST or self.object_path(digest).exists():
                result.append(self.object_path(digest))
            if node["kind"] == "branch":
                for slot, child in reversed(node["children"]):
                    pending.append((child, depth + 1, prefix + format(slot, "x")))
        return tuple(result)

    def _normalize_sets(self, values: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(values, Mapping):
            raise ProjectionIntegrityError("sets_must_be_mapping")
        normalized: dict[str, Any] = {}
        for key, value in values.items():
            self._validate_key(key)
            encoded_value = canonical_json_bytes(value)
            decoded_value = _decode_json_strict(encoded_value)
            single_leaf = self._leaf_payload({key: decoded_value})
            if len(single_leaf) > MAX_OBJECT_BYTES:
                raise ProjectionObjectLimitError("object_bytes")
            normalized[key] = decoded_value
        return normalized

    def _normalize_deletes(self, values: Iterable[str]) -> set[str]:
        if isinstance(values, (str, bytes)):
            raise ProjectionIntegrityError("deletes_must_be_key_iterable")
        try:
            normalized = set(values)
        except TypeError as exc:
            raise ProjectionIntegrityError("deletes_invalid") from exc
        for key in normalized:
            self._validate_key(key)
        return normalized

    def _validate_key(self, key: str) -> str:
        if not isinstance(key, str):
            raise ProjectionIntegrityError("key_must_be_string")
        encoded = key.encode("utf-8")
        if len(encoded) > MAX_LEAF_BYTES // 2:
            raise ProjectionObjectLimitError("key_bytes")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _validate_digest(digest: str) -> None:
        if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
            raise ProjectionIntegrityError("digest_invalid")

    @staticmethod
    def _validate_read_bounds(limit: int, max_objects: int, maximum: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= maximum:
            raise ProjectionObjectLimitError("read_limit")
        if (
            isinstance(max_objects, bool)
            or not isinstance(max_objects, int)
            or not 1 <= max_objects <= DEFAULT_READ_OBJECT_LIMIT * 16
        ):
            raise ProjectionObjectLimitError("read_object_limit")

    def _mutate_subtree(
        self,
        digest: str,
        *,
        depth: int,
        prefix: str,
        sets: dict[str, Any],
        deletes: set[str],
        reads: _ReadContext,
        writes: _WriteStats,
    ) -> str:
        node, node_size = self._load_node(digest, depth, prefix, reads)
        if not sets and not deletes:
            writes.record_reused(digest, node_size)
            return digest

        if node["kind"] == "leaf":
            updated = {key: value for key, value in node["entries"]}
            before = canonical_json_bytes(updated)
            for key in deletes:
                updated.pop(key, None)
            updated.update(sets)
            if before == canonical_json_bytes(updated):
                writes.record_reused(digest, node_size)
                return digest
            if not updated:
                writes.record_reused(EMPTY_ROOT_DIGEST, len(canonical_json_bytes(_EMPTY_LEAF)))
                return EMPTY_ROOT_DIGEST
            return self._build_subtree(updated, depth, prefix, writes)

        children = dict(node["children"])
        set_groups: dict[int, dict[str, Any]] = {}
        delete_groups: dict[int, set[str]] = {}
        for key, value in sets.items():
            slot = int(key_digest(key)[depth], 16)
            set_groups.setdefault(slot, {})[key] = value
        for key in deletes:
            slot = int(key_digest(key)[depth], 16)
            delete_groups.setdefault(slot, set()).add(key)

        changed = False
        for slot in sorted(set(set_groups).union(delete_groups)):
            child_sets = set_groups.get(slot, {})
            child_deletes = delete_groups.get(slot, set())
            old_child = children.get(slot)
            if old_child is None:
                if not child_sets:
                    continue
                new_child = self._build_subtree(
                    child_sets,
                    depth + 1,
                    prefix + format(slot, "x"),
                    writes,
                )
            else:
                new_child = self._mutate_subtree(
                    old_child,
                    depth=depth + 1,
                    prefix=prefix + format(slot, "x"),
                    sets=child_sets,
                    deletes=child_deletes,
                    reads=reads,
                    writes=writes,
                )
            if new_child == old_child:
                continue
            changed = True
            if new_child == EMPTY_ROOT_DIGEST:
                children.pop(slot, None)
            else:
                children[slot] = new_child

        if not changed:
            writes.record_reused(digest, node_size)
            return digest
        if not children:
            writes.record_reused(EMPTY_ROOT_DIGEST, len(canonical_json_bytes(_EMPTY_LEAF)))
            return EMPTY_ROOT_DIGEST
        return self._store_node(self._branch_node(children), writes)

    def _build_subtree(
        self,
        entries: dict[str, Any],
        depth: int,
        prefix: str,
        writes: _WriteStats,
    ) -> str:
        leaf = self._leaf_node(entries)
        leaf_payload = canonical_json_bytes(leaf)
        leaf_byte_limit = (
            MAX_OBJECT_BYTES if len(entries) == 1 else MAX_LEAF_BYTES
        )
        if len(entries) <= MAX_LEAF_ENTRIES and len(leaf_payload) <= leaf_byte_limit:
            return self._store_node(leaf, writes)
        if depth >= MAX_DEPTH:
            raise ProjectionDepthError(f"depth_limit:{depth}")

        groups: dict[int, dict[str, Any]] = {}
        for key, value in entries.items():
            digest = key_digest(key)
            if not digest.startswith(prefix):
                raise ProjectionIntegrityError("entry_prefix_mismatch")
            slot = int(digest[depth], 16)
            groups.setdefault(slot, {})[key] = value
        children = {
            slot: self._build_subtree(
                child_entries,
                depth + 1,
                prefix + format(slot, "x"),
                writes,
            )
            for slot, child_entries in sorted(groups.items())
        }
        return self._store_node(self._branch_node(children), writes)

    @staticmethod
    def _leaf_node(entries: Mapping[str, Any]) -> dict[str, Any]:
        ordered = sorted(entries.items(), key=lambda item: _entry_order(item[0]))
        return {
            "entries": [[key, value] for key, value in ordered],
            "kind": "leaf",
            "version": FORMAT_VERSION,
        }

    @classmethod
    def _leaf_payload(cls, entries: Mapping[str, Any]) -> bytes:
        return canonical_json_bytes(cls._leaf_node(entries))

    @staticmethod
    def _branch_node(children: Mapping[int, str]) -> dict[str, Any]:
        return {
            "children": [[slot, digest] for slot, digest in sorted(children.items())],
            "kind": "branch",
            "version": FORMAT_VERSION,
        }

    def _store_node(self, node: dict[str, Any], writes: _WriteStats) -> str:
        payload = canonical_json_bytes(node)
        if len(payload) > MAX_OBJECT_BYTES:
            raise ProjectionObjectLimitError("object_bytes")
        if (
            node.get("kind") == "leaf"
            and len(node.get("entries") or []) != 1
            and len(payload) > MAX_LEAF_BYTES
        ):
            raise ProjectionObjectLimitError("leaf_bytes")
        digest = hashlib.sha256(payload).hexdigest()
        if digest == EMPTY_ROOT_DIGEST:
            writes.record_reused(digest, len(payload))
            return digest
        target = self.object_path(digest)
        if self._lexists(target):
            existing = self._secure_read_object(target, digest)
            if existing != payload:
                raise ProjectionIntegrityError("content_address_collision")
            writes.record_reused(digest, len(payload))
            return digest

        self._ensure_layout(target.parent, writes)
        if self._lexists(target):
            existing = self._secure_read_object(target, digest)
            if existing != payload:
                raise ProjectionIntegrityError("content_address_collision")
            writes.record_reused(digest, len(payload))
            return digest

        temporary = target.with_name(
            f"{target.name}.tmp-{os.getpid()}-{threading.get_ident()}-"
            f"{secrets.token_hex(8)}"
        )
        self._assert_contained(temporary, target.parent)
        promoted = False
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= int(getattr(os, "O_BINARY", 0))
            flags |= int(getattr(os, "O_NOFOLLOW", 0))
            descriptor = os.open(temporary, flags, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = -1
                    handle.write(payload)
                    durability.sync_open_file(handle)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            promoted = self._promote_object(temporary, target, payload)
        finally:
            if self._lexists(temporary):
                os.unlink(temporary)
            # The content file was flushed before the hard link was created.
            # One directory barrier after removing the temporary name durably
            # commits the final target name and the cleanup together.  Flushing
            # both the link and unlink separately doubled the dominant Windows
            # latency without providing a stronger post-return guarantee.
            if promoted:
                durability.sync_directory(temporary.parent)
        if promoted:
            writes.record_new(digest, len(payload))
        else:
            writes.record_reused(digest, len(payload))
        return digest

    def _promote_object(
        self,
        temporary: Path,
        target: Path,
        payload: bytes,
    ) -> bool:
        """Atomically create ``target`` without replacing an existing object."""
        self._assert_secure_file(temporary)
        self._assert_secure_directory(target.parent)
        try:
            os.link(temporary, target)
        except FileExistsError:
            existing = self._secure_read_object(
                target,
                hashlib.sha256(payload).hexdigest(),
            )
            if existing != payload:
                raise ProjectionIntegrityError("content_address_collision")
            return False
        return True

    def _load_node(
        self,
        digest: str,
        depth: int,
        prefix: str,
        reads: _ReadContext,
    ) -> tuple[dict[str, Any], int]:
        self._validate_digest(digest)
        if depth > MAX_DEPTH:
            raise ProjectionDepthError(f"depth_limit:{depth}")
        cached = reads.raw_cache.get(digest)
        if cached is None:
            path = self.object_path(digest)
            if digest == EMPTY_ROOT_DIGEST and not self._lexists(path):
                node = dict(_EMPTY_LEAF)
                size = len(canonical_json_bytes(node))
            else:
                if reads.objects_read >= reads.max_objects:
                    raise ProjectionObjectLimitError("read_object_limit")
                payload = self._secure_read_object(path, digest, reads=reads)
                reads.objects_read += 1
                decoded = _decode_json_strict(payload)
                if payload != canonical_json_bytes(decoded):
                    raise ProjectionIntegrityError("noncanonical_object")
                if not isinstance(decoded, dict):
                    raise ProjectionIntegrityError("node_not_object")
                node = decoded
                size = len(payload)
            reads.raw_cache[digest] = (node, size)
        else:
            node, size = cached
        self._validate_node(node, size, depth, prefix)
        return node, size

    def _validate_node(
        self,
        node: dict[str, Any],
        size: int,
        depth: int,
        prefix: str,
    ) -> None:
        if node.get("version") != FORMAT_VERSION:
            raise ProjectionIntegrityError("node_version")
        kind = node.get("kind")
        if kind == "leaf":
            if set(node) != {"entries", "kind", "version"}:
                raise ProjectionIntegrityError("leaf_shape")
            entries = node["entries"]
            if not isinstance(entries, list):
                raise ProjectionIntegrityError("leaf_entries")
            if len(entries) > MAX_LEAF_ENTRIES:
                raise ProjectionIntegrityError("leaf_entry_limit")
            leaf_byte_limit = (
                MAX_OBJECT_BYTES if len(entries) == 1 else MAX_LEAF_BYTES
            )
            if size > leaf_byte_limit:
                raise ProjectionObjectLimitError("leaf_bytes")
            previous: tuple[str, str] | None = None
            for entry in entries:
                if not isinstance(entry, list) or len(entry) != 2:
                    raise ProjectionIntegrityError("leaf_entry_shape")
                key = entry[0]
                order = (self._validate_key(key), key)
                if previous is not None and order <= previous:
                    raise ProjectionIntegrityError("leaf_key_order")
                if not order[0].startswith(prefix):
                    raise ProjectionIntegrityError("leaf_key_prefix")
                previous = order
            return
        if kind != "branch":
            raise ProjectionIntegrityError("node_kind")
        if set(node) != {"children", "kind", "version"}:
            raise ProjectionIntegrityError("branch_shape")
        if depth >= MAX_DEPTH:
            raise ProjectionDepthError(f"depth_limit:{depth}")
        children = node["children"]
        if not isinstance(children, list) or not children or len(children) > 16:
            raise ProjectionIntegrityError("branch_children")
        previous_slot = -1
        for child in children:
            if not isinstance(child, list) or len(child) != 2:
                raise ProjectionIntegrityError("branch_child_shape")
            slot, digest = child
            if isinstance(slot, bool) or not isinstance(slot, int) or not 0 <= slot < 16:
                raise ProjectionIntegrityError("branch_slot")
            if slot <= previous_slot:
                raise ProjectionIntegrityError("branch_slot_order")
            self._validate_digest(digest)
            if digest == EMPTY_ROOT_DIGEST:
                raise ProjectionIntegrityError("branch_empty_child")
            previous_slot = slot

    def _iter_subtree(
        self,
        digest: str,
        depth: int,
        prefix: str,
        reads: _ReadContext,
    ) -> Iterator[tuple[str, Any]]:
        node, _size = self._load_node(digest, depth, prefix, reads)
        if node["kind"] == "leaf":
            yield from ((entry[0], entry[1]) for entry in node["entries"])
            return
        for slot, child in node["children"]:
            yield from self._iter_subtree(
                child,
                depth + 1,
                prefix + format(slot, "x"),
                reads,
            )

    def _secure_read_object(
        self,
        path: Path,
        digest: str,
        *,
        reads: _ReadContext | None = None,
    ) -> bytes:
        # All call sites pass a path produced by ``object_path``.  That helper
        # admits only a validated hex digest and therefore constructs a path
        # beneath ``objects_dir`` by construction.
        if reads is None:
            self._assert_secure_directory(path.parent)
        else:
            self._assert_secure_read_directory(path.parent, reads)
        try:
            before = os.lstat(path)
        except OSError as exc:
            raise ProjectionIntegrityError(f"object_missing:{digest}") from exc
        if _is_reparse(before) or stat.S_ISLNK(before.st_mode):
            raise ProjectionSecurityError("object_is_redirect")
        if not stat.S_ISREG(before.st_mode):
            raise ProjectionSecurityError("object_not_regular")
        flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ProjectionIntegrityError(f"object_open_failed:{digest}") from exc
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise ProjectionSecurityError("opened_object_not_regular")
            if before.st_dev != opened.st_dev or before.st_ino != opened.st_ino:
                raise ProjectionSecurityError("object_changed_during_open")
            declared_size = int(opened.st_size)
            if declared_size > MAX_OBJECT_BYTES:
                raise ProjectionObjectLimitError("object_bytes")
            chunks: list[bytes] = []
            remaining = declared_size
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            if (
                remaining
                or opened.st_dev != after.st_dev
                or opened.st_ino != after.st_ino
                or declared_size != int(after.st_size)
            ):
                raise ProjectionSecurityError("object_changed_during_read")
            if len(chunks) == 1:
                payload = chunks[0]
            else:
                payload = b"".join(chunks)
        finally:
            os.close(descriptor)
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ProjectionIntegrityError("hash_mismatch")
        return payload

    def _assert_secure_read_directory(
        self,
        path: Path,
        reads: _ReadContext,
    ) -> None:
        """Amortize path resolution without weakening in-read race checks."""
        key = _normal_path(path)
        expected = reads.secure_directories.get(key)
        if expected is None:
            self._assert_secure_directory(path)
            info = os.lstat(path)
            reads.secure_directories[key] = (int(info.st_dev), int(info.st_ino))
            reads.secure_directory_paths[key] = path
            return
        if reads.defer_directory_rechecks:
            return
        self._assert_secure_read_directory_identity(path, expected)

    def _verify_secure_read_directories(self, reads: _ReadContext) -> None:
        if not reads.defer_directory_rechecks:
            return
        for key, expected in sorted(reads.secure_directories.items()):
            self._assert_secure_read_directory_identity(
                reads.secure_directory_paths[key],
                expected,
            )

    @staticmethod
    def _assert_secure_read_directory_identity(
        path: Path,
        expected: tuple[int, int],
    ) -> None:
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise ProjectionSecurityError(f"directory_missing:{path}") from exc
        if _is_reparse(info) or stat.S_ISLNK(info.st_mode):
            raise ProjectionSecurityError(f"directory_is_redirect:{path}")
        if not stat.S_ISDIR(info.st_mode):
            raise ProjectionSecurityError(f"directory_not_directory:{path}")
        if (int(info.st_dev), int(info.st_ino)) != expected:
            raise ProjectionSecurityError(f"directory_changed_during_read:{path}")

    def _ensure_layout(self, target_parent: Path, writes: _WriteStats) -> None:
        self._ensure_directory(self.base_dir, writes)
        self._ensure_directory(self.store_dir, writes)
        self._ensure_directory(self.store_dir / "objects", writes)
        self._ensure_directory(self.objects_dir, writes)
        self._ensure_directory(target_parent, writes)
        self._assert_contained(target_parent, self.objects_dir)

    def _ensure_directory(self, path: Path, writes: _WriteStats) -> None:
        if self._lexists(path):
            self._assert_secure_write_directory(path, writes)
            return
        missing: list[Path] = []
        cursor = path
        while not self._lexists(cursor):
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                raise ProjectionSecurityError("directory_root_missing")
            cursor = parent
        self._assert_secure_write_directory(cursor, writes)
        for candidate in reversed(missing):
            try:
                candidate.mkdir()
            except FileExistsError:
                pass
            self._assert_secure_write_directory(candidate, writes)
            durability.sync_directory(candidate.parent)

    def _assert_secure_write_directory(
        self,
        path: Path,
        writes: _WriteStats,
    ) -> None:
        """Amortize resolution while retaining per-use directory identity checks."""
        key = _normal_path(path)
        expected = writes.secure_directories.get(key)
        if expected is None:
            self._assert_secure_directory(path)
            info = os.lstat(path)
            writes.secure_directories[key] = (int(info.st_dev), int(info.st_ino))
            return
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise ProjectionSecurityError(f"directory_missing:{path}") from exc
        if _is_reparse(info) or stat.S_ISLNK(info.st_mode):
            raise ProjectionSecurityError(f"directory_is_redirect:{path}")
        if not stat.S_ISDIR(info.st_mode):
            raise ProjectionSecurityError(f"directory_not_directory:{path}")
        if (int(info.st_dev), int(info.st_ino)) != expected:
            raise ProjectionSecurityError(f"directory_changed_during_write:{path}")

    def _assert_secure_directory(self, path: Path) -> None:
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise ProjectionSecurityError(f"directory_missing:{path}") from exc
        if _is_reparse(info) or stat.S_ISLNK(info.st_mode):
            raise ProjectionSecurityError(f"directory_is_redirect:{path}")
        if not stat.S_ISDIR(info.st_mode):
            raise ProjectionSecurityError(f"directory_not_directory:{path}")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ProjectionSecurityError(f"directory_resolve_failed:{path}") from exc
        if _normal_path(resolved) != _normal_path(path):
            raise ProjectionSecurityError(f"directory_redirected:{path}")

    @staticmethod
    def _assert_secure_file(path: Path) -> None:
        try:
            info = os.lstat(path)
        except OSError as exc:
            raise ProjectionSecurityError(f"file_missing:{path}") from exc
        if _is_reparse(info) or stat.S_ISLNK(info.st_mode):
            raise ProjectionSecurityError(f"file_is_redirect:{path}")
        if not stat.S_ISREG(info.st_mode):
            raise ProjectionSecurityError(f"file_not_regular:{path}")

    @staticmethod
    def _assert_contained(path: Path, root: Path) -> None:
        try:
            common = os.path.commonpath((_normal_path(path), _normal_path(root)))
        except ValueError as exc:
            raise ProjectionSecurityError("path_drive_mismatch") from exc
        if common != _normal_path(root):
            raise ProjectionSecurityError("path_outside_store")

    @staticmethod
    def _lexists(path: Path) -> bool:
        return os.path.lexists(os.fspath(path))


__all__ = [
    "DEFAULT_READ_OBJECT_LIMIT",
    "DiffEntry",
    "DiffResult",
    "EMPTY_ROOT_DIGEST",
    "FORMAT_VERSION",
    "MAX_BATCH_MUTATIONS",
    "MAX_DEPTH",
    "MAX_DIFF_LIMIT",
    "MAX_ITER_LIMIT",
    "MAX_LEAF_BYTES",
    "MAX_LEAF_ENTRIES",
    "MAX_OBJECT_BYTES",
    "MutationResult",
    "ProjectionDepthError",
    "ProjectionIntegrityError",
    "ProjectionObjectLimitError",
    "ProjectionSecurityError",
    "ProjectionStoreError",
    "ProjectionStoreV2",
    "canonical_json_bytes",
    "key_digest",
]
