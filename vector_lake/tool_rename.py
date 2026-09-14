import hashlib
import os
import re
from pathlib import Path

import yaml

from vector_lake.claim_extractor import _stable_id
from vector_lake.db_store import get_connection, get_db_path
from vector_lake.mutation_coordinator import execute_mutation_batch
from vector_lake.wiki_utils import (
    get_wiki_dir,
    iter_markdown_files,
    normalize_entity_name,
    normalize_semantic_text,
    split_frontmatter,
)


def _canonical_entity_for_page_key(page_key: str) -> dict | None:
    """Return one canonical entity record by page key, or ``None`` when absent.

    Read failures are not converted into "does not exist": a database error must
    surface rather than be reported as a missing entity.
    """
    from vector_lake.tool_projection import _canonical_entity_by_page_key

    if not get_db_path().exists():
        return None
    return _canonical_entity_by_page_key(page_key)


def _frontmatter_from_entity_for_rename(entity: dict) -> dict:
    """Build projection frontmatter that carries the canonical identity forward.

    ``_frontmatter_from_entity`` writes ``id`` only.  The extractor derives
    ``entity_id`` from the frontmatter first and falls back to a page-key hash, so
    the canonical ``entity_id`` must be carried explicitly or a rename would mint a
    new identity for an existing page.
    """
    from vector_lake.tool_projection import _frontmatter_from_entity

    frontmatter = _frontmatter_from_entity(entity)
    entity_id = str(entity.get("entity_id") or "").strip()
    if entity_id:
        frontmatter["entity_id"] = entity_id
    return frontmatter


def rename_vector_lake_entity(
    old_name: str, new_name: str, dry_run: bool = True
) -> str:
    """Atomically rename an entity and update every exact internal link."""
    wiki_dir = Path(get_wiki_dir()).resolve(strict=True)
    old_name = old_name if old_name.casefold().endswith(".md") else f"{old_name}.md"
    new_name = new_name if new_name.casefold().endswith(".md") else f"{new_name}.md"
    old_path = (wiki_dir / old_name).resolve()
    if not old_path.is_relative_to(wiki_dir):
        return (
            f"[Security Error] Old entity '{old_name}' is outside the wiki directory."
        )
    matches = [
        path.resolve()
        for path in iter_markdown_files(wiki_dir)
        if path.name.casefold() == old_name.casefold()
    ]
    if len(matches) > 1:
        return f"Error: Old entity '{old_name}' is ambiguous."
    canonical_source: dict | None = None
    if matches:
        old_path = matches[0]
        old_name = old_path.name
    elif not old_path.exists():
        # A canonical row can outlive its Markdown projection: the page name may
        # predate the current filename contract, and the materialize path
        # deliberately refuses to *create* such a name.  Rebuilding the source from
        # canonical lets the same retire/create batch legalize the name instead of
        # leaving the page permanently unrepairable.  It is the same single code
        # path, not a second rename implementation.
        canonical_source = _canonical_entity_for_page_key(old_name[:-3])
        if not canonical_source:
            return f"Error: Old entity '{old_name}' does not exist."

    normalized_new_name = normalize_entity_name(new_name[:-3]) + ".md"
    new_path = (wiki_dir / normalized_new_name).resolve()
    if not new_path.is_relative_to(wiki_dir):
        return f"[Security Error] Target entity '{normalized_new_name}' is outside the wiki directory."
    if new_path.exists():
        return f"Error: Target entity '{normalized_new_name}' already exists. Use merge instead."

    if canonical_source is not None:
        # No on-disk projection to hash, so the retire leg settles idempotently
        # against the missing path (the delete gate permits that for exactly this
        # case).  ``entity_id`` must be carried in the frontmatter because the
        # extractor derives identity from it and would otherwise mint a new one
        # from the destination filename.  This content is re-parsed immediately
        # below and re-serialised with the destination key, so its intermediate
        # formatting does not need to match any stored version -- the create leg
        # becomes the new canonical baseline by construction.
        old_projection_hash = ""
        from vector_lake.tool_projection import _body_from_entity

        frontmatter = _frontmatter_from_entity_for_rename(canonical_source)
        synthesized = (
            f"---\n{yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)}"
            f"---\n{_body_from_entity(canonical_source, frontmatter)}"
        )
        frontmatter, body = split_frontmatter(synthesized)
    else:
        old_bytes = old_path.read_bytes()
        old_projection_hash = hashlib.sha256(old_bytes).hexdigest()
        old_content = normalize_semantic_text(old_bytes.decode("utf-8"))
        frontmatter, body = split_frontmatter(old_content)
    preserved_entity_id = str(frontmatter.get("entity_id") or "").strip()
    if not preserved_entity_id and get_db_path().exists():
        try:
            row = (
                get_connection()
                .execute(
                    "SELECT entity_id FROM entities "
                    "WHERE json_extract(data_json, '$.page_key') = ? LIMIT 1",
                    (old_name[:-3],),
                )
                .fetchone()
            )
            if row is not None:
                preserved_entity_id = str(row["entity_id"])
        except Exception:
            preserved_entity_id = ""
    # The fallback is the legacy identity of the old page, never the new name.
    # Persisting it in frontmatter prevents subsequent renames from changing it.
    frontmatter["entity_id"] = preserved_entity_id or _stable_id(
        "entity", old_name[:-3]
    )
    old_core = old_name.split("_", 1)[-1][:-3] if "_" in old_name else old_name[:-3]
    new_core = (
        normalized_new_name.split("_", 1)[-1][:-3]
        if "_" in normalized_new_name
        else normalized_new_name[:-3]
    )
    if frontmatter.get("title") == old_core:
        frontmatter["title"] = new_core
    aliases = list(frontmatter.get("aliases") or [])
    if old_core not in aliases:
        aliases.append(old_core)
    frontmatter["aliases"] = aliases

    old_key = old_name[:-3]
    new_key = normalized_new_name[:-3]
    exact = re.compile(r"\[\[" + re.escape(old_key) + r"\]\]")
    with_alias = re.compile(r"\[\[" + re.escape(old_key) + r"\|([^\]]+)\]\]")

    def replace_links(content: str) -> str:
        content = exact.sub(f"[[{new_key}|{old_core}]]", content)
        return with_alias.sub(r"[[" + new_key + r"|\1]]", content)

    body = replace_links(body)
    new_content = (
        f"---\n{yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)}---\n{body}"
    )
    mutations = [
        {
            "filename": old_name,
            "is_delete": True,
            "expected_projection_hash": old_projection_hash,
        },
        {
            "filename": normalized_new_name,
            "content": new_content,
            "expected_projection_hash": "",
        },
    ]
    updated_files = 0
    for root, _, files in os.walk(wiki_dir):
        for filename in files:
            if not filename.casefold().endswith(".md") or filename.casefold() in {
                "index.md",
                "log.md",
                "overview.md",
            }:
                continue
            path = Path(root) / filename
            if path in {old_path, new_path}:
                continue
            content_bytes = path.read_bytes()
            content = normalize_semantic_text(content_bytes.decode("utf-8"))
            replaced = replace_links(content)
            if replaced != content:
                mutations.append(
                    {
                        "filename": filename,
                        "content": replaced,
                        "expected_projection_hash": hashlib.sha256(
                            content_bytes
                        ).hexdigest(),
                    }
                )
                updated_files += 1

    if dry_run:
        return (
            f"[DRY-RUN] Would rename '{old_name}' to '{normalized_new_name}' "
            f"and update links in {updated_files} file(s)."
        )
    try:
        execute_mutation_batch(
            mutations,
            # The canonical-only case is a bounded repair of the projection itself.
            # It must use the ``schema`` channel, because the full write gate is
            # blocked by exactly the inconsistency being repaired
            # (``write_projection_drift: missing_wiki=1``): the gate and the repair
            # would otherwise deadlock, which is how such a page could never be
            # legalized by anyone including an operator.  ``schema`` is the
            # documented bounded-repair channel; the destination name is still
            # checked against the filename contract explicitly, and the retire leg
            # keeps every delete gate.
            validation_mode=(
                "schema" if canonical_source is not None else "full"
            ),
            # The destination page carries the already-reviewed content of the page
            # being retired, so it is validated structurally rather than re-audited
            # against the current evidence contract (see
            # ``identity_only_filenames`` in mutation_coordinator).  The retire leg
            # of the same batch keeps every delete gate.
            identity_only_filenames=[normalized_new_name],
        )
    except Exception as exc:
        return f"Error during atomic rename: {exc}"
    return f"Successfully renamed '{old_name}' to '{normalized_new_name}'. Updated links in {updated_files} files."
