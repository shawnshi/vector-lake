import yaml

from vector_lake.wiki_utils import split_frontmatter


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def merge_markdown_content(left_content: str, right_content: str) -> str:
    """Return a merged left page without mutating either source file."""
    left_frontmatter, left_body = split_frontmatter(left_content)
    right_frontmatter, right_body = split_frontmatter(right_content)
    if not left_frontmatter:
        raise ValueError("The merge target has no valid YAML frontmatter.")
    if not right_frontmatter:
        raise ValueError("The merge source has no valid YAML frontmatter.")

    left_aliases = _as_list(left_frontmatter.get("aliases"))
    right_aliases = _as_list(right_frontmatter.get("aliases"))
    right_title = str(right_frontmatter.get("title") or "").strip()
    if right_title:
        right_aliases.append(right_title)
    for alias in right_aliases:
        if alias and alias not in left_aliases:
            left_aliases.append(alias)
    left_frontmatter["aliases"] = left_aliases

    rendered_frontmatter = yaml.safe_dump(
        left_frontmatter,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    source_label = right_title or "merged source"
    return (
        f"---\n{rendered_frontmatter}---\n"
        f"{left_body.strip()}\n\n## Merged from {source_label}\n{right_body.strip()}\n"
    )
