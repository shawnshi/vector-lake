"""Which page a link names, and which names are ambiguous -- one owner for both.

``tool_lint`` decided this for the broken-link report; ``indexer`` decided it again for the graph
edges, with a different rule.  Two answers to one question is how the same link came to be "fine"
to lint and dropped by the graph:

* the graph resolved a target against filenames, titles and aliases only, so a link by *core* name
  -- ``[[Concept_CoMET]]`` where ``Product_CoMET.md`` exists -- built no edge, while lint accepted
  it after that rule was added there.  Measured on the live wiki: 67 typed links were dropped this
  way, 23 of which the core rule resolves;
* the graph's declaration map was last-writer-wins (a title or alias could overwrite another page's
  filename), which is the defect ``tool_lint`` had already fixed for its own map;
* the graph also accepted a page's ``id`` as a link target.  An id is an identifier, not a name --
  nothing writes a link that way deliberately, and accepting one silently resolves a link no reader
  can explain.  Measured: zero links on the live wiki depend on it, so refusing it costs nothing.

The rules, and why each is what it is:

* **an exact filename wins**, then a title or alias declared by exactly one page, then the target's
  core name when exactly one page carries it (``_``/``-``/space are one name, as everywhere else);
* **a name two pages declare or carry is left unresolved** rather than resolved to whichever page
  happens to sort first: the duplication is a real defect, and picking a winner hides it;
* **``System_*`` pages and the non-node artifacts are excluded from the core map** even though their
  files exist: resolving to one would satisfy a check while the indexer deletes that page from the
  graph, which is the "hide the gap" trade the stub creator refuses on the other side.

This module has no dependencies beyond the vocabulary and the name helpers, so both the linter and
the indexer can import it without creating a cycle.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping

from vector_lake.node_vocabulary import (
    NON_NODE_WIKI_FILES,
    strip_prefix,
)
from vector_lake.wiki_utils import entity_identity_key


def declaration_map(claims: Mapping[str, Iterable[str]]) -> dict[str, str]:
    """``name -> page`` for the names exactly one page declares as a title or alias.

    A contested name is omitted: it stays unresolved, and the caller reports the claimants.  This
    is the one implementation of that rule; callers only differ in how they collect ``claims``.
    """
    return {name: next(iter(pages)) for name, pages in claims.items() if len(set(pages)) == 1}


def core_name_maps(node_keys: Iterable[str]) -> tuple[dict[str, list[str]], dict[str, str]]:
    """``(core -> pages, unambiguous core -> page)`` for the pages a link may resolve to.

    Built together because they answer one question from two sides: the first names the contested
    cores, the second is what resolution uses.
    """
    core_pages: dict[str, list[str]] = defaultdict(list)
    for node_key in node_keys:
        if node_key.startswith("System_") or f"{node_key}.md" in NON_NODE_WIKI_FILES:
            continue
        core_pages[entity_identity_key(strip_prefix(node_key))].append(node_key)
    return core_pages, {core: pages[0] for core, pages in core_pages.items() if len(pages) == 1}


def resolve_link_target(
    target: str, link_map: Mapping[str, str], unique_cores: Mapping[str, str]
) -> str | None:
    """The page ``target`` names, or ``None`` when no page answers it.

    Two lookups, and the order is the design: the map as written (filename, unique title, unique
    alias), then the target's core name in the unique-cores map.  A name two pages claim is in
    neither, so it stays unresolved and the caller can say why.
    """
    resolved = link_map.get(target)
    if resolved:
        return resolved
    return unique_cores.get(entity_identity_key(strip_prefix(target)))


def build_link_map(
    node_keys: Iterable[str], claims: Mapping[str, Iterable[str]]
) -> tuple[dict[str, str], dict[str, list[str]], dict[str, str], set[str]]:
    """``(link map, core -> pages, unique cores, contested names)`` for a set of pages.

    Four values because every caller needs some of them and they must agree: the linter resolves
    links and names the claimants of a contested one; the indexer builds edges and the alias index
    the topology layer reads.
    """
    link_map = {node_key: node_key for node_key in node_keys}
    for name, page in declaration_map(claims).items():
        link_map.setdefault(name, page)
    core_pages, unique_cores = core_name_maps(node_keys)
    contested = {core for core, pages in core_pages.items() if len(pages) > 1}
    contested |= {name for name, pages in claims.items() if len(set(pages)) > 1}
    return link_map, core_pages, unique_cores, contested
