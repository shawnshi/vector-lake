import hashlib
import logging
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from vector_lake import db_store
from vector_lake.defense_hook import verify_asset
from vector_lake.schema_validator import validate_schema
from vector_lake.wiki_utils import (
    atomic_write_text,
    delete_file_compare_and_swap,
    get_index_path,
    get_outbox_signal_path,
    get_wiki_dir,
    split_frontmatter,
    validate_wiki_filename,
)


log = logging.getLogger("vector-lake-mutation")


def _markdown_page_key(filename: str) -> str:
    return filename[:-3] if filename.casefold().endswith(".md") else filename


def _filename_identity(filename: str) -> str:
    return unicodedata.normalize("NFKC", filename).casefold()


def _mutation_idempotency_key(mutation: dict, validation_mode: str) -> str:
    projection_base_hash = mutation["projection_base_hash"]
    token = (
        f"{mutation['mutation_type']}\x00{mutation['filename']}\x00"
        f"{mutation['content'] or ''}"
        + (
            f"\x00expected_version={mutation['expected_version']}"
            if mutation["has_expected_version"]
            else ""
        )
        + (f"\x00validation={validation_mode}" if validation_mode != "full" else "")
        + (
            f"\x00projection_base_hash={projection_base_hash}"
            if projection_base_hash is not None
            else ""
        )
    )
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def resolve_wiki_mutation_path(
    filename: str,
    allow_existing_legacy_name: bool = False,
) -> Path:
    """Resolve a single wiki basename and reject every traversal form."""
    if not isinstance(filename, str) or not filename or filename != filename.strip():
        raise ValueError("Mutation filename must be a non-empty trimmed string.")
    security_name = unicodedata.normalize("NFKC", filename)
    if any(
        marker in candidate
        for candidate in (filename, security_name)
        for marker in ("\x00", "/", "\\")
    ):
        raise ValueError(
            "Mutation filename must be a basename without path separators."
        )
    candidate_name = Path(filename)
    if (
        candidate_name.is_absolute()
        or candidate_name.name != filename
        or filename in {".", ".."}
    ):
        raise ValueError("Mutation filename must be a relative wiki basename.")
    wiki_root = get_wiki_dir().resolve()
    candidate = (wiki_root / filename).resolve()
    if candidate.parent != wiki_root:
        raise ValueError(f"Mutation path escapes wiki boundary: {filename}")
    if not (allow_existing_legacy_name and candidate.exists()):
        validate_wiki_filename(filename)
    return candidate


def materialize_markdown_projection(
    filename: str,
    mutation_type: str,
    payload_text: str | None = None,
    validation_mode: str = "full",
    projection_base_hash: str | None = None,
) -> Path:
    """Idempotently materialize the Markdown projection for one outbox row."""
    filepath = resolve_wiki_mutation_path(
        filename,
        allow_existing_legacy_name=validation_mode == "schema",
    )
    if mutation_type == "delete":
        if filepath.exists():
            delete_file_compare_and_swap(filepath, projection_base_hash)
        return filepath
    if mutation_type != "update":
        raise ValueError(f"Unsupported mutation_type: {mutation_type}")
    if payload_text is None:
        if not filepath.exists():
            raise ValueError(
                f"Outbox update for {filename} has no payload and no existing Markdown projection."
            )
        payload_text = filepath.read_text(encoding="utf-8")
    if filepath.exists() and projection_base_hash is not None:
        current_hash = hashlib.sha256(filepath.read_bytes()).hexdigest()
        desired_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        if current_hash == desired_hash:
            return filepath
    atomic_write_text(
        filepath,
        payload_text,
        validation_mode=validation_mode,
        expected_current_hash=projection_base_hash,
    )
    return filepath


def _signal_outbox_consumer() -> str | None:
    """Best-effort wake-up hint; correctness never depends on this file."""
    try:
        signal_path = get_outbox_signal_path()
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(signal_path, "1")
    except Exception as exc:
        warning = (
            f"Outbox wake-up hint failed after commit: {type(exc).__name__}: {exc}"
        )
        log.warning(warning)
        return warning
    return None


def _prepare_mutations(
    mutations: Iterable[dict],
    validation_mode: str = "full",
) -> list[dict]:
    if validation_mode not in {"full", "schema"}:
        raise ValueError(f"Unsupported validation_mode: {validation_mode}")
    prepared = []
    seen_filenames: dict[str, str] = {}
    existing_filenames: dict[str, set[str]] = {}
    wiki_dir = get_wiki_dir()
    existing_paths = wiki_dir.iterdir() if wiki_dir.exists() else ()
    for existing_path in existing_paths:
        if not existing_path.is_file() or existing_path.suffix.casefold() != ".md":
            continue
        existing_filenames.setdefault(
            _filename_identity(existing_path.name),
            set(),
        ).add(existing_path.name)
    for mutation in mutations:
        filename = mutation.get("filename")
        if not isinstance(filename, str):
            raise ValueError("Mutation filename must be a non-empty trimmed string.")
        filename_identity = _filename_identity(filename)
        if filename_identity in seen_filenames:
            raise ValueError(
                "A mutation batch cannot contain duplicate filenames: "
                f"{seen_filenames[filename_identity]} and {filename}"
            )
        existing_aliases = existing_filenames.get(filename_identity, set())
        if len(existing_aliases) > 1:
            raise ValueError(
                "Mutation filename has multiple Unicode/case-equivalent existing pages: "
                + ", ".join(sorted(existing_aliases))
            )
        if existing_aliases and filename not in existing_aliases:
            existing_name = next(iter(existing_aliases))
            raise ValueError(
                f"Mutation filename is an alias of existing page: "
                f"{filename} -> {existing_name}"
            )
        content = mutation.get("content")
        is_delete = bool(mutation.get("is_delete", False))
        has_expected_version = "expected_version" in mutation
        expected_version = mutation.get("expected_version")
        projection_base_hash = mutation.get("expected_projection_hash")
        if has_expected_version and not isinstance(expected_version, str):
            raise ValueError("expected_version must be a string when supplied.")
        if projection_base_hash not in {None, ""} and (
            not isinstance(projection_base_hash, str)
            or len(projection_base_hash) != 64
            or any(
                character not in "0123456789abcdef"
                for character in projection_base_hash.casefold()
            )
        ):
            raise ValueError(
                "expected_projection_hash must be empty or a 64-character SHA-256 hex digest."
            )
        filepath = resolve_wiki_mutation_path(
            filename,
            allow_existing_legacy_name=validation_mode == "schema",
        )
        if projection_base_hash is not None:
            projection_base_hash = projection_base_hash.casefold()

        seen_filenames[filename_identity] = filename

        mutation_type = "delete" if is_delete else "update"
        if not is_delete:
            if content is None:
                raise ValueError("Update mutations require full Markdown content.")
            frontmatter, _ = split_frontmatter(content)
            if validation_mode == "full":
                verify_asset(content, filename, frontmatter, get_index_path())
            else:
                # Schema mode is a bounded legacy-maintenance path. Dynamic
                # tag/entity collision checks belong to full writes because
                # existing pages may predate the current index taxonomy.
                validate_schema(frontmatter, content, filename)

        prepared.append(
            {
                "filename": filename,
                "content": content,
                "filepath": filepath,
                "mutation_type": mutation_type,
                "has_expected_version": has_expected_version,
                "expected_version": expected_version,
                "projection_base_hash": projection_base_hash,
                "idempotency_key": None,
            }
        )
    if not prepared:
        raise ValueError("A mutation batch must contain at least one mutation.")
    return prepared


def execute_mutation_batch(
    mutations: Iterable[dict],
    canonical_callback: Callable[[], None] | None = None,
    validation_mode: str = "full",
    origin: str = "mutation_coordinator",
    return_details: bool = False,
    transaction_callback: Callable[[list[int]], None] | None = None,
    precondition_callback: Callable[[], None] | None = None,
):
    """Commit canonical mutations atomically.

    With ``return_details=True``, ``committed`` becomes true only after the
    database transaction exits successfully. Ordinary post-commit failures are
    returned in ``post_commit_warnings`` and never redefine durable commit state.
    Schema mode is for bounded legacy maintenance.
    """
    from vector_lake.runtime_health import enforce_runtime_write_health

    enforce_runtime_write_health(validation_mode=validation_mode)
    prepared = _prepare_mutations(mutations, validation_mode=validation_mode)
    db_store.init_db()
    outbox_ids = []
    from vector_lake import governance_store

    prepared_change_sets = []
    for mutation in prepared:
        if mutation["mutation_type"] != "update":
            continue
        filename = mutation["filename"]
        change_set = governance_store.prepare_change_set_from_content(
            filename,
            mutation["content"],
            origin=origin,
            auto_approve=True,
            summary=f"Canonical mutation for {filename}",
        )
        prepared_change_sets.append(change_set)

    with db_store.transaction():
        page_keys = {_markdown_page_key(mutation["filename"]) for mutation in prepared}
        base_versions = governance_store.canonical_page_versions(page_keys)
        versioned = [
            mutation for mutation in prepared if mutation["has_expected_version"]
        ]
        if versioned:
            page_keys = {
                _markdown_page_key(mutation["filename"]) for mutation in versioned
            }
            current_versions = governance_store.canonical_page_versions(page_keys)
            for mutation in versioned:
                filename = mutation["filename"]
                page_key = _markdown_page_key(filename)
                expected = mutation["expected_version"]
                current = current_versions.get(page_key)
                if (expected == "" and current is not None) or (
                    expected != "" and current != expected
                ):
                    raise ValueError(
                        f"Canonical version conflict for {filename}: "
                        f"expected {expected or '<absent>'}, current {current or '<absent>'}"
                    )
        if precondition_callback is not None:
            precondition_callback()
        for mutation in prepared:
            filepath = mutation["filepath"]
            current_projection_hash = (
                hashlib.sha256(filepath.read_bytes()).hexdigest()
                if filepath.exists()
                else ""
            )
            if mutation["projection_base_hash"] is None:
                mutation["projection_base_hash"] = current_projection_hash
            elif mutation["projection_base_hash"] != current_projection_hash:
                raise RuntimeError(
                    "Projection changed before canonical mutation commit for "
                    f"{mutation['filename']}: expected "
                    f"{mutation['projection_base_hash'] or '<absent>'}, current "
                    f"{current_projection_hash or '<absent>'}"
                )
            mutation["idempotency_key"] = _mutation_idempotency_key(
                mutation,
                validation_mode,
            )
        for mutation in prepared:
            filename = mutation["filename"]
            content = mutation["content"]
            if mutation["mutation_type"] == "delete":
                node_key = _markdown_page_key(filename)
                db_store.delete_node_cascade(node_key)
        governance_store.apply_change_sets_batch(prepared_change_sets)
        published_at = datetime.now(timezone.utc).isoformat()
        for change_set in prepared_change_sets:
            change_set["published_at"] = published_at
        governance_store.record_prepared_change_sets(prepared_change_sets)

        for mutation in prepared:
            filename = mutation["filename"]
            content = mutation["content"]
            outbox_ids.append(
                db_store.enqueue_mutation(
                    filename,
                    mutation["mutation_type"],
                    payload_text=content,
                    idempotency_key=mutation["idempotency_key"],
                    validation_mode=validation_mode,
                    base_version=base_versions.get(
                        _markdown_page_key(filename),
                        "",
                    ),
                    projection_base_hash=mutation["projection_base_hash"],
                )
            )
        if canonical_callback is not None:
            canonical_callback()
        if transaction_callback is not None:
            transaction_callback(list(outbox_ids))

    deferred = []
    post_commit_warnings = []
    for mutation, outbox_id in zip(prepared, outbox_ids):
        try:
            with db_store.transaction():
                if not db_store.mutation_outbox_is_latest_intent(outbox_id):
                    deferred.append(mutation["filename"])
                    continue
                materialize_markdown_projection(
                    mutation["filename"],
                    mutation["mutation_type"],
                    mutation["content"],
                    validation_mode=validation_mode,
                    projection_base_hash=mutation["projection_base_hash"],
                )
        except Exception as exc:
            deferred.append(mutation["filename"])
            post_commit_warnings.append(
                "Markdown projection failed after commit for "
                f"{mutation['filename']}: {type(exc).__name__}: {exc}"
            )
            log.error(
                "Canonical mutation %s committed but projection failed for %s: %s",
                outbox_id,
                mutation["filepath"],
                exc,
            )

    try:
        signal_warning = _signal_outbox_consumer()
    except Exception as exc:
        # Keep the commit boundary truthful even when a monkeypatched or
        # replacement notifier violates the best-effort helper contract.
        signal_warning = (
            f"Outbox wake-up hint failed after commit: {type(exc).__name__}: {exc}"
        )
        log.warning(signal_warning)
    if signal_warning:
        post_commit_warnings.append(signal_warning)
    projection_note = (
        f"projections deferred for {', '.join(deferred)}"
        if deferred
        else "all Markdown projections materialized"
    )
    warning_note = (
        f"; {len(post_commit_warnings)} post-commit warning(s) recorded"
        if post_commit_warnings
        else ""
    )
    message = (
        f"Canonical mutation batch committed; outbox ids {outbox_ids}; "
        f"{projection_note}{warning_note}."
    )
    if return_details:
        return {
            "ok": True,
            "committed": True,
            "message": message,
            "outbox_ids": outbox_ids,
            "deferred": deferred,
            "post_commit_warnings": post_commit_warnings,
        }
    return True, message


def execute_mutation_plan(
    filename: str, content: str | None = None, is_delete: bool = False
):
    """Commit one canonical mutation and durable intent before updating projections."""
    return execute_mutation_batch(
        [{"filename": filename, "content": content, "is_delete": is_delete}]
    )
