import os
import re
from pathlib import Path

import yaml

from vector_lake.mutation_coordinator import execute_mutation_batch
from vector_lake.wiki_utils import get_wiki_dir, normalize_entity_name, read_markdown_file


def rename_vector_lake_entity(old_name: str, new_name: str, dry_run: bool = True) -> str:
    """Atomically rename an entity and update every exact internal link."""
    wiki_dir = Path(get_wiki_dir()).resolve(strict=True)
    old_name = old_name if old_name.endswith(".md") else f"{old_name}.md"
    new_name = new_name if new_name.endswith(".md") else f"{new_name}.md"
    old_path = (wiki_dir / old_name).resolve()
    if not old_path.is_relative_to(wiki_dir):
        return f"[Security Error] Old entity '{old_name}' is outside the wiki directory."
    if not old_path.exists():
        return f"Error: Old entity '{old_name}' does not exist."

    normalized_new_name = normalize_entity_name(new_name[:-3]) + ".md"
    new_path = (wiki_dir / normalized_new_name).resolve()
    if not new_path.is_relative_to(wiki_dir):
        return f"[Security Error] Target entity '{normalized_new_name}' is outside the wiki directory."
    if new_path.exists():
        return f"Error: Target entity '{normalized_new_name}' already exists. Use merge instead."

    frontmatter, body, _ = read_markdown_file(old_path)
    old_core = old_name.split("_", 1)[-1][:-3] if "_" in old_name else old_name[:-3]
    new_core = normalized_new_name.split("_", 1)[-1][:-3] if "_" in normalized_new_name else normalized_new_name[:-3]
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
    new_content = f"---\n{yaml.dump(frontmatter, allow_unicode=True, sort_keys=False)}---\n{body}"
    mutations = [
        {"filename": old_name, "is_delete": True},
        {"filename": normalized_new_name, "content": new_content},
    ]
    updated_files = 0
    for root, _, files in os.walk(wiki_dir):
        for filename in files:
            if not filename.endswith(".md") or filename in {"index.md", "log.md", "overview.md"}:
                continue
            path = Path(root) / filename
            if path in {old_path, new_path}:
                continue
            content = path.read_text(encoding="utf-8")
            replaced = replace_links(content)
            if replaced != content:
                mutations.append({"filename": filename, "content": replaced})
                updated_files += 1

    if dry_run:
        return (
            f"[DRY-RUN] Would rename '{old_name}' to '{normalized_new_name}' "
            f"and update links in {updated_files} file(s)."
        )
    try:
        execute_mutation_batch(mutations)
    except Exception as exc:
        return f"Error during atomic rename: {exc}"
    return f"Successfully renamed '{old_name}' to '{normalized_new_name}'. Updated links in {updated_files} files."
