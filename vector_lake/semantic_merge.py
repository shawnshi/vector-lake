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

Frontmatter is a union, not an inheritance: the survivor keeps its identity
(``id``, ``title``, ``created``) and its own values, and the consumed page's
evidence metadata is *added*.  Inheriting the survivor's frontmatter verbatim --
what this function used to do, apart from aliases -- deleted the consumed page's
``sources``, ``tags`` and ``categories`` along with its file, which is silent
evidence loss in a provenance-first store.  Two rules bound the union:

* additive fields (``aliases``, ``sources``, ``tags``) keep the survivor's order and
  then append what the consumed page adds -- including the consumed page's **filename**,
  which is why ``merge_markdown_content`` takes a ``consumed_page_key`` argument;
* ``tags`` is capped at ``schema_validator.MAX_TAGS`` with the survivor's tags
  preferred, because the write gate rejects an over-long list anyway; the drop is
  logged rather than performed silently;
* ``categories`` is single-valued despite being stored as a list (the purpose contract
  requires exactly one domain), so the survivor keeps its own and the union is never
  attempted -- see ``_ADDITIVE_FIELDS``.

Everything else the consumed page declares and the survivor does not is copied
across (``evidence_tier``, ``tension_edges``, ...), so a field cannot disappear
just because the survivor never used it.
"""
import logging
import re

import yaml

from vector_lake.wiki_utils import split_frontmatter
from vector_lake.schema_validator import (
    MAX_TAGS,
    TENSION_H3_SLOT,
    VALID_H3_SLOTS,
)

log = logging.getLogger("vector-lake-semantic-merge")

# Values that accumulate across a merge instead of being replaced.  Dropping any of
# these is the defect described in the module docstring.
#
# ``categories`` is deliberately **not** here even though the write gate stores it as a
# list.  ``purpose_contract.validate_ingest_payload`` requires it to be a list with
# *exactly one* domain, so it is a single-valued field wearing a list: unioning two pages'
# categories produces a page the gate then refuses.  It looked additive when this list was
# written and only a merge between two pages with different domains exposed it -- the
# survivor keeps its own domain, which is also the only answer consistent with the rule.
_ADDITIVE_FIELDS = ("aliases", "sources", "tags")

# Single-valued fields expressed as lists.  The survivor's value wins outright; the
# consumed page's value is not merged in and cannot make the union invalid.
_SINGLE_VALUE_LIST_FIELDS = ("categories",)

# Fields that describe *which entity this page is*.  They are never inherited from
# the consumed page: `id` and `title` would re-label the survivor as the thing that
# was merged into it, and a copied `created` would rewrite its history.
_IDENTITY_FIELDS = frozenset({"id", "title", "created"})

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
    """Split a body into ``(prefix, heading1, section1, heading2, section2)``.

    ``prefix`` is whatever precedes the first ``## 1.`` heading -- in practice the
    page H1.  It used to be dropped here, so a merged page lost the title line its
    two inputs both carried.  Returns ``None`` when the body is not dual-schema,
    which keeps the pre-existing single-block behaviour for pages that carry no
    profile sections.
    """
    heading_1 = _SECTION_1.search(body)
    heading_2 = _SECTION_2.search(body)
    if not heading_1 or not heading_2 or heading_2.start() < heading_1.end():
        return None
    section_1 = body[heading_1.end():heading_2.start()]
    # A stray separator inside Section 1 would make the validator stop reading
    # Section 1 early; the merged page emits exactly one, before Section 2.
    section_1 = _SEPARATOR.sub("", section_1)
    return (
        body[:heading_1.start()].strip(),
        heading_1.group(0).strip(),
        section_1,
        heading_2.group(0).strip(),
        body[heading_2.end():],
    )


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


def _newer_stamp(left_value, right_value):
    """The later of two ``updated`` stamps, compared on their date part.

    Deliberately not ``max()`` over the raw strings: the store holds both
    ``'2026-09-12'`` and ``'2026-09-07T08:40:25.890500+00:00'``, and a merge must not
    move the survivor's timestamp backwards just because the consumed page wrote a
    longer form of an earlier day.
    """
    left_stamp, right_stamp = str(left_value or ""), str(right_value or "")
    if not left_stamp:
        return right_value
    if not right_stamp:
        return left_value
    return right_value if right_stamp[:10] > left_stamp[:10] else left_value


def _union_frontmatter(
    left_frontmatter: dict,
    right_frontmatter: dict,
    consumed_page_key: str | None = None,
) -> dict:
    """Fold the consumed page's frontmatter into the survivor's, in place.

    The survivor's values win wherever the two disagree, except for ``updated``,
    which must not go backwards.  See the module docstring for why inheritance alone
    is evidence loss.

    ``consumed_page_key`` is the *filename* of the page being merged away.  It has to be
    passed in rather than read from the frontmatter, because it appears in neither the
    title nor the alias list of most pages -- and without it every ``[[ConsumedKey]]`` in
    the wiki goes dangling the moment the page is merged away.  Measured on the live
    corpus: 321 links across 33 of the 3 793 merges on record, all of which had to be
    repaired by hand afterwards.
    """
    right_title = str(right_frontmatter.get("title") or "").strip()

    # The consumed page's title and its own key are both more names for the same entity,
    # so they join the alias set -- this is also what keeps inbound ``[[Old_Page_Name]]``
    # links resolvable after the consumed file is deleted.
    consumed_names = _as_list(right_frontmatter.get("aliases"))
    if right_title:
        consumed_names.append(right_title)
    if consumed_page_key:
        consumed_names.append(consumed_page_key)
    additive = {"aliases": consumed_names}
    for field in _ADDITIVE_FIELDS:
        if field == "aliases":
            continue
        additive[field] = _as_list(right_frontmatter.get(field))
    for field, right_values in additive.items():
        merged = _as_list(left_frontmatter.get(field))
        for value in right_values:
            if value and value not in merged:
                merged.append(value)
        if merged:
            left_frontmatter[field] = merged

    tags = left_frontmatter.get("tags")
    if isinstance(tags, list) and len(tags) > MAX_TAGS:
        # The write gate rejects the page above the cap, so the union has to be cut
        # here.  The survivor's tags are kept first: they are the ones its own
        # sources were curated against.  Logged, because a silent truncation is the
        # same class of defect as the silent drop this function was fixing.
        dropped = tags[MAX_TAGS:]
        left_frontmatter["tags"] = tags[:MAX_TAGS]
        log.warning(
            "Merge tag union exceeds MAX_TAGS=%s; dropped %s",
            MAX_TAGS,
            ", ".join(str(tag) for tag in dropped),
        )

    if "updated" in right_frontmatter:
        left_frontmatter["updated"] = _newer_stamp(
            left_frontmatter.get("updated"), right_frontmatter.get("updated")
        )

    for field, value in right_frontmatter.items():
        if field in _IDENTITY_FIELDS or field in _ADDITIVE_FIELDS or field == "updated":
            continue
        if field in _SINGLE_VALUE_LIST_FIELDS:
            continue
        if field not in left_frontmatter and value not in (None, "", [], {}):
            left_frontmatter[field] = value

    return left_frontmatter


def merge_markdown_content(
    left_content: str,
    right_content: str,
    consumed_page_key: str | None = None,
) -> str:
    """Return a merged left page without mutating either source file.

    ``consumed_page_key`` is the consumed page's filename stem; pass it so the name stops
    resolving when its file does.  Omitting it preserves the previous behaviour, which is
    only correct when nothing links to the consumed page by key.
    """
    left_frontmatter, left_body = split_frontmatter(left_content)
    right_frontmatter, right_body = split_frontmatter(right_content)
    if not left_frontmatter:
        raise ValueError("The merge target has no valid YAML frontmatter.")
    if not right_frontmatter:
        raise ValueError("The merge source has no valid YAML frontmatter.")

    right_title = str(right_frontmatter.get("title") or "").strip()
    _union_frontmatter(left_frontmatter, right_frontmatter, consumed_page_key)

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
        prefix, heading_1, section_1, heading_2, section_2 = left_sections
        right_sections = _split_sections(right_body)
        right_section_1, right_section_2 = (right_sections[2], right_sections[4]) if right_sections else (right_body, "")
        merged_body = "\n\n".join(part for part in [
            prefix,
            heading_1,
            _merge_section_1(section_1, right_section_1, _allowed_h3_slots(left_frontmatter)),
            "---",
            heading_2,
            _merge_section_2(section_2, right_section_2),
            f"## Merged from {source_label}",
            f"本页由 {source_label} 合并而来；其编译事实与时间线条目已并入上文对应分节。",
        ] if part)
    return (
        f"---\n{rendered_frontmatter}---\n"
        f"{merged_body.strip()}\n"
    )
