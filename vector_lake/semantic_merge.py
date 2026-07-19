import re

import yaml

from vector_lake.wiki_utils import split_frontmatter


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _demote_headings(body: str) -> str:
    """Keep source labels without leaving heading tokens that mimic a timeline section."""
    lines = []
    for line in body.splitlines():
        match = re.match(r"^(#{1,6})(\s+.*)$", line)
        if match:
            line = f"**{match.group(2).strip()}**"
        lines.append(line)
    return "\n".join(lines).strip()


def merge_markdown_content(
    left_content: str,
    right_content: str,
    source_key: str | None = None,
) -> str:
    """Return a merged left page without mutating either source file."""
    left_frontmatter, left_body = split_frontmatter(left_content)
    right_frontmatter, right_body = split_frontmatter(right_content)
    if not left_frontmatter:
        raise ValueError("The merge target has no valid YAML frontmatter.")
    if not right_frontmatter:
        raise ValueError("The merge source has no valid YAML frontmatter.")

    left_aliases = _as_list(left_frontmatter.get("aliases"))
    right_aliases = _as_list(right_frontmatter.get("aliases"))
    if source_key:
        right_aliases.append(source_key)
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
    source_label = right_title or source_key or "merged source"
    source_block = f"## Merged from {source_label}\n{_demote_headings(right_body)}"
    timeline_match = re.search(r"(?m)^## 2\. 证据时间线.*$", left_body)
    if timeline_match:
        prefix = left_body[: timeline_match.start()].rstrip()
        timeline = left_body[timeline_match.start() :].lstrip()
        merged_body = f"{prefix}\n\n{source_block}\n\n{timeline}"
    else:
        merged_body = f"{left_body.strip()}\n\n{source_block}"
    return (
        f"---\n{rendered_frontmatter}---\n"
        f"{merged_body.strip()}\n"
    )
