"""The wiki node-type vocabulary, in one place.

A node type appears in three shapes across the codebase, and they had drifted
apart because each site spelled the list out by hand:

* the **prefix** a wiki filename starts with -- ``Vendor_Epic-Systems.md`` --
  used by ``wiki_utils`` for filename validation, ``indexer`` for what to index,
  ``tool_lint`` for what to check and ``governance_service`` for which filenames a
  page owns;
* the **bare type** written into frontmatter (``type: vendor``), used by
  ``schema_validator`` to reject unknown types and by the broken-link stub creator
  to label a stub;
* the **file stem** minus its prefix, used to decide whether a link target already
  has a page.

``tool_query`` kept its own copy of the prefix list and, in a second place in the
same file, an inline literal of it. Both were missing ``System_`` while the code
comment claimed to mirror ``wiki_utils.VALID_PREFIXES``. The consequence was not
cosmetic: a link to an existing ``System_Community_*`` page did not match that page
by stem, and a stub invented for a ``System_*`` target was typed ``concept``.

``NON_NODE_WIKI_FILES`` is here for the same reason and covers the other half of
the question.  A node type answers "what kind of node is this file?"; this set
answers "is this file a node at all?".  It had been spelled out six times, and the
sites did not agree on what to do with a name that is missing from it, so an
answer that is not written down in one place is an answer that drifts.

This module has no imports, so every layer can depend on it without creating a
cycle or dragging in the wiki filesystem helpers.
"""

#: Wiki files that are not knowledge nodes: they are index and report artifacts the
#: wiki generates about itself, so they are skipped by the page projection, the graph
#: indexer and the schema validator alike.  ``wiki_utils.SYSTEM_WHITELIST`` is this
#: same object.
#:
#: ``System_*`` files are the *other* way to be a non-node, and deliberately are not
#: listed here: they are matched by prefix, and ``GENERATED_NODE_TYPES`` already
#: records that their type is generated.
#:
#: Not to be confused with the ``{index.md, log.md, overview.md}`` lists a number of
#: call sites pass when choosing which files to *read or delete*.  That is a narrower
#: question with its own answer, and it is not this set.
NON_NODE_WIKI_FILES: frozenset[str] = frozenset({
    "index.md",
    "log.md",
    "overview.md",
    "orphan_pages.md",
    "wiki_link_stats.md",
    "Synthesis_log.md",
})

#: Canonical order.  ``wiki_utils.VALID_PREFIXES`` is derived from it, so the
#: order of this tuple is part of that public tuple's value.
NODE_TYPES: tuple[str, ...] = (
    "concept",
    "vendor",
    "institution",
    "product",
    "person",
    "event",
    "policy",
    "standard",
    "source",
    "synthesis",
    "system",
)


def prefix_for(node_type: str) -> str:
    """``"vendor"`` -> ``"Vendor_"``."""
    return node_type[:1].upper() + node_type[1:] + "_"


def type_for_prefix(prefix: str) -> str | None:
    """``"Vendor_"`` -> ``"vendor"``, or ``None`` if it is not one of ours."""
    if not prefix.endswith("_"):
        return None
    candidate = prefix[:-1].lower()
    return candidate if candidate in NODE_TYPES else None


#: Every prefix a wiki filename may start with, in canonical order.
NODE_PREFIXES: tuple[str, ...] = tuple(prefix_for(node_type) for node_type in NODE_TYPES)

#: The bare types, for frontmatter validation.
NODE_TYPE_SET: frozenset[str] = frozenset(NODE_TYPES)

#: ``Concept|Vendor|...|System``, for callers that must build a regex instead of
#: using ``str.startswith``.  Derived for the same reason as the prefix tuple: the
#: hand-written alternation in ``wiki_utils.validate_wiki_filename`` was missing
#: ``System`` (harmless there only because an earlier line returns early for that
#: prefix, which is exactly the kind of coincidence that hides drift).  No entry is
#: a prefix of another, so alternation order cannot change what matches.
NODE_TYPE_ALTERNATION: str = "|".join(
    prefix[:-1] for prefix in NODE_PREFIXES
)

#: Types whose pages are generated artifacts.  ``indexer`` skips them and they are
#: absent from the page projection, so a link to one cannot be resolved by
#: creating a page: the file would satisfy the linter while staying invisible to
#: the graph, hiding the gap instead of reporting it.  Callers that materialise
#: pages must refuse these types rather than label a page with them.
GENERATED_NODE_TYPES: frozenset[str] = frozenset({"system"})


def type_for_node_id(node_id: str) -> str | None:
    """The declared type of a node id such as ``Vendor_Epic-Systems``.

    Returns ``None`` when the id carries no known type prefix, which is distinct
    from a caller's own default: the broken-link stub creator needs to tell "this
    is not a typed node" from "this is a typed node whose type it must use".
    """
    for prefix in NODE_PREFIXES:
        if node_id.startswith(prefix):
            return type_for_prefix(prefix)
    return None


def strip_prefix(node_id: str) -> str:
    """``"Vendor_Epic-Systems"`` -> ``"Epic-Systems"`` (unchanged if untyped)."""
    for prefix in NODE_PREFIXES:
        if node_id.startswith(prefix):
            return node_id[len(prefix):]
    return node_id
