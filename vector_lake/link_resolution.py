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


def declared_names_from_nodes(nodes: Mapping[str, Mapping]) -> dict[str, list[str]]:
    """``{declared name -> pages}`` for a node mapping whose values carry title and aliases.

    One owner for the declaration rule.  The indexer's full build, its incremental path and the
    linter's frontmatter pass all need this map, and two of them were copies of the same loop.
    """
    declared: dict[str, list[str]] = {}
    for key, node in nodes.items():
        title = node.get("title")
        if title:
            declared.setdefault(str(title).strip(), []).append(str(key))
        aliases = node.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        for alias in aliases:
            declared.setdefault(str(alias).strip(), []).append(str(key))
    return declared


def core_name_maps(
    node_keys: Iterable[str], declared: Mapping[str, Iterable[str]] | None = None
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """``(core -> pages, unambiguous core -> page)`` for the pages a link may resolve to.

    Built together because they answer one question from two sides: the first names the contested
    cores, the second is what resolution uses.

    ``declared`` (the title/alias map the flat link map is built from) folds those names into the
    same table, because a link may reach a page by *any* declared name -- and until it did, the core
    fallback answered only page names, so a link whose sole match was an alias's core name stayed
    unresolved while the identical spelling of a page name resolved.  Names are added at the same
    *rank* as page names, so a name two pages claim makes that core contested; resolution refuses a
    contested core, which is the rule the flat map already applies through ``declaration_map``.
    """
    core_pages: dict[str, list[str]] = defaultdict(list)
    present: set[str] = set()
    for node_key in node_keys:
        node_key = str(node_key)
        if node_key.startswith("System_") or f"{node_key}.md" in NON_NODE_WIKI_FILES:
            continue
        present.add(node_key)
        core_pages[entity_identity_key(strip_prefix(node_key))].append(node_key)
    for name, pages in (declared or {}).items():
        key = entity_identity_key(strip_prefix(str(name)))
        if key in core_pages:
            # An alias must not be able to contest a name some page *owns*.  Folding every declared
            # name in unconditionally took the live contested-key count from 36 to 185 and left 11
            # links unresolved that had resolved before, because one page's alias shadowed another
            # page's own name.  Own names win; the flat map applies the same precedence by seeding
            # itself with the page keys.
            continue
        for page in sorted({str(page) for page in pages}):
            if page in present:
                core_pages[key].append(page)
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
    core_pages, unique_cores = core_name_maps(node_keys, claims)
    contested = {core for core, pages in core_pages.items() if len(pages) > 1}
    contested |= {name for name, pages in claims.items() if len(set(pages)) > 1}
    return link_map, core_pages, unique_cores, contested
