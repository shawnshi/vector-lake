"""Section-aware merge of two Vector Lake page bodies.

The previous implementation concatenated the whole right body under a
``## Merged from X`` heading.  That output cannot pass ``schema_validator``: the
event-store check runs ``## 2. 证据时间线`` to end of file and requires every
bullet after it to be a dated event, so the consumed page's compiled-truth
bullets were read as malformed timeline entries.  Merging therefore only worked
if the consumed page had first been overwritten with a bullet-free placeholder.

Merging now distributes both bodies into exactly one Section 1 and one Section 2,
so the concatenation itself is schema-conformant.  The function stays pure: it
never touches either input string.
"""
import re

import yaml

from vector_lake.wiki_utils import split_frontmatter
from vector_lake.schema_validator import TENSION_H3_SLOT, VALID_H3_SLOTS

_SECTION_1 = re.compile(r"^##\s*1\.\s*编译事实.*$", re.MULTILINE)
_SECTION_2 = re.compile(r"^##\s*2\.\s*证据时间线.*$", re.MULTILINE)
_H3 = re.compile(r"^###\s+(.*)$")
_SEPARATOR = re.compile(r"^\s*---\s*$", re.MULTILINE)


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _split_sections(body: str):
    """Split a body into (heading1, section1, heading2, section2).

    Returns ``None`` when the body is not dual-schema, which keeps the pre-existing
    single-block behaviour for pages that carry no profile sections.
    """
    heading_1 = _SECTION_1.search(body)
    heading_2 = _SECTION_2.search(body)
    if not heading_1 or not heading_2 or heading_2.start() < heading_1.end():
        return None
    section_1 = body[heading_1.end():heading_2.start()]
    # A stray separator inside Section 1 would make the validator stop reading
    # Section 1 early; the merged page emits exactly one, before Section 2.
    section_1 = _SEPARATOR.sub("", section_1)
    return heading_1.group(0).strip(), section_1, heading_2.group(0).strip(), body[heading_2.end():]


def _split_h3_blocks(text: str):
    """Return (preamble, [(heading, body), ...]) preserving block order."""
    preamble: list = []
    blocks: list = []
    for line in text.splitlines():
        match = _H3.match(line)
        if match:
            blocks.append([f"### {match.group(1).rstrip()}", []])
        elif blocks:
            blocks[-1][1].append(line)
        else:
            preamble.append(line)
    return "\n".join(preamble).strip(), [(head, "\n".join(body).strip()) for head, body in blocks]


def _allowed_h3_slots(frontmatter: dict) -> set:
    """Mirror ``schema_validator``'s Section-1 allow-list for this page.

    ``TENSION_H3_SLOT`` is conditional on ``tension_edges``: demoting it would strip
    a slot the validator then demands, which is how the first version of this
    section-aware merge broke pages that carry cognitive-tension edges.
    """
    slots = set(VALID_H3_SLOTS.get(str(frontmatter.get("type", "") or "").lower(), []))
    if frontmatter.get("tension_edges"):
        slots.add(TENSION_H3_SLOT)
    return slots


def _merge_section_1(left_text: str, right_text: str, allowed: set) -> str:
    left_preamble, left_blocks = _split_h3_blocks(left_text)
    right_preamble, right_blocks = _split_h3_blocks(right_text)

    parts = []
    seen_preamble_lines = set()
    preamble_blocks = []
    for text in (left_preamble, right_preamble):
        block = []
        for line in text.splitlines():
            if not line.strip() or line in seen_preamble_lines:
                continue
            seen_preamble_lines.add(line)
            block.append(line.rstrip())
        if block:
            preamble_blocks.append("\n".join(block))
    if preamble_blocks:
        parts.append("\n\n".join(preamble_blocks))

    order, merged = [], {}
    for heading, block in left_blocks + right_blocks:
        if heading not in merged:
            order.append(heading)
            merged[heading] = []
        for line in block.splitlines():
            if line.strip() and line not in merged[heading]:
                merged[heading].append(line)

    for heading in order:
        body = "\n".join(merged[heading]).strip()
        label = heading[4:].strip()
        # A slot that is valid for the consumed page's type may be invalid for the
        # surviving page's type.  Demote the heading rather than drop the facts or
        # emit a heading the write gate rejects.
        if not allowed or heading in allowed:
            parts.append(f"{heading}\n{body}" if body else heading)
        else:
            parts.append(f"**{label}**\n{body}" if body else f"**{label}**")
    return "\n\n".join(parts).strip()


def _merge_section_2(left_text: str, right_text: str) -> str:
    """Union both event ledgers, dropping exact duplicates and blank lines."""
    lines, seen = [], set()
    for text in (left_text, right_text):
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped in seen:
                continue
            seen.add(stripped)
            lines.append(line.rstrip())
    return "\n".join(lines).strip()


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
    left_sections = _split_sections(left_body)
    if left_sections is None:
        merged_body = f"{left_body.strip()}\n\n## Merged from {source_label}\n{right_body.strip()}"
    else:
        heading_1, section_1, heading_2, section_2 = left_sections
        right_sections = _split_sections(right_body)
        right_section_1, right_section_2 = (right_sections[1], right_sections[3]) if right_sections else (right_body, "")
        merged_body = "\n\n".join([
            heading_1,
            _merge_section_1(section_1, right_section_1, _allowed_h3_slots(left_frontmatter)),
            "---",
            heading_2,
            _merge_section_2(section_2, right_section_2),
            f"## Merged from {source_label}",
            f"本页由 {source_label} 合并而来；其编译事实与时间线条目已并入上文对应分节。",
        ])
    return (
        f"---\n{rendered_frontmatter}---\n"
        f"{merged_body.strip()}\n"
    )
