"""One owner for what a broken-link stub page is: its name, type, fields and write.

``tool_lint --auto-fix`` and ``tool_query``'s finalisation each created stubs for broken
links with separate code, and the two disagreed on every decision:

========================================  ==============================  ==============================
decision                                   ``tool_lint``                   ``tool_query``
========================================  ==============================  ==============================
filename for an untyped target             ``Concept_<target>.md``         ``<target>.md``
``id``                                     generated, e.g. ``20260918_``  the target name
                                           ``0f9t54``
an existing page under another prefix      not checked: ``[[Epic-         checked, so nothing is
                                           Systems]]`` next to             written
                                           ``Vendor_Epic-Systems.md``
                                           produced
                                           ``Concept_Epic-Systems.md``
write path                                 ``write_markdown_file``,        ``execute_mutation_plan``
                                           which validates the filename,   with a hand-built YAML
                                           the schema and the compiled-    string
                                           truth guards
========================================  ==============================  ==============================

Both outcomes were wrong, and both are reproduced in ``tests/test_stub_creator.py``:
``tool_lint`` forked one entity into two nodes, and ``tool_query`` created **nothing at all**
-- an unprefixed ``<target>.md`` is not a valid node filename, so its write was refused and the
exception swallowed into a warning.

The rules below are therefore a choice, not a synthesis, and each is stated with its source:

* **The prefix and the frontmatter type are one decision, taken from the vocabulary**
  (``tool_lint``'s rule -- the variant that actually validates).  An untyped target becomes
  ``Concept_<target>.md``; a typed one keeps its prefix.  ``wiki_utils.validate_wiki_filename``
  is what rejects the alternative.
* **A generated type is refused outright** (both callers agreed).  ``indexer`` skips
  ``System_*`` pages, so a stub would satisfy the broken-link check while staying invisible to
  the graph: the gap would stop being reported without ever being closed.
* **A page whose core name already exists under any prefix blocks the write**
  (``tool_query``'s rule; ``tool_lint`` lacked it).  ``Vendor_Epic-Systems.md`` covers
  ``Epic-Systems``, and adding ``Concept_Epic-Systems.md`` beside it forks the entity.
* **The write goes through** ``write_markdown_file`` (``tool_lint``'s path).  It validates the
  filename, the schema and the frontmatter/body guards, none of which the hand-built YAML
  string went past.

``id`` is generated rather than set to the page name, following the live wiki: ids there are
free-form and stable (``20260602_idamp``, ``20260616_6m307a``) and 59 of 60 sampled pages
differ from their filename, so ``tool_query``'s ``id = target`` was the outlier.  The two
optional fields ``tool_query`` carried are kept -- ``topic_cluster`` and
``tags: ["auto-stub"]`` -- because they are valid, they change no requirement, and the tag is
how an operator finds pages nothing sourced.
"""

from __future__ import annotations

import datetime
import logging
import os
import random
import re
import string

from vector_lake.node_vocabulary import (
    GENERATED_NODE_TYPES,
    prefix_for,
    strip_prefix,
    type_for_node_id,
)
from vector_lake.schema_validator import VALID_H3_SLOTS
from vector_lake.wiki_utils import normalize_entity_name, write_markdown_file

log = logging.getLogger("vector-lake-stub-creator")

#: Characters no wiki filename may contain, plus the underscore: ``validate_wiki_filename``
#: rejects these (``wiki_utils.INVALID_CHARS_REGEX``) and its strict pattern allows no
#: underscore beyond the single one after the type prefix, so a name built from a link target
#: has to lose them before it can be valid.  Mirrored here rather than imported because
#: ``wiki_utils`` builds those rules into a filename check, not into a transformation.
_FORBIDDEN_FILENAME_CHARS = re.compile(r'[\[\]<>:"/\\|\?\*\(\)\s_]+')

#: Slot used when a type declares no H3 slots of its own.
_FALLBACK_SLOT = "### 物理机制 (Mechanism)"

#: What a stub declares that no rule derives from the target.
_STUB_MARKER_TAG = "auto-stub"


def _generate_id(today: str) -> str:
    """``20260918_ab12cd`` -- the shape the live wiki's free-form ids already use."""
    return f"{today.replace('-', '')}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=6))}"


def stub_type(target: str) -> str:
    """The type a stub for ``target`` declares: its own, or ``concept`` when untyped.

    The untyped default is why an untyped target's *filename* has to gain a prefix: the type
    and the prefix are one decision (:func:`stub_page_name`).
    """
    return type_for_node_id(target) or "concept"


def existence_index(wiki_dir: str) -> tuple[set[str], set[str], dict[str, str]]:
    """``(stems, normalized stems, core name -> stem)`` for every page in ``wiki_dir``.

    Built once per pass and updated in place by :func:`create_stub`, so creating several stubs
    in one run cannot fork its own output.
    """
    stems = {name[:-3] for name in os.listdir(wiki_dir) if name.endswith(".md")}
    normalized = {normalize_entity_name(stem) for stem in stems}
    cores = {strip_prefix(stem): stem for stem in stems}
    return stems, normalized, cores


def covering_page(target: str, index: tuple[set[str], set[str], dict[str, str]]) -> str | None:
    """The page that already covers ``target``, or ``None`` when nothing does.

    A match is either the target itself (exact or normalised) or the page carrying the same
    core name under a different type prefix.  This is the rule ``tool_lint`` was missing: a
    link to ``[[Epic-Systems]]`` must resolve to ``Vendor_Epic-Systems.md`` rather than
    justify a second page.
    """
    if not target:
        return None
    stems, normalized, cores = index
    if target in normalized or target in stems:
        return target
    return cores.get(strip_prefix(target))


def _sanitize_core(name: str) -> str:
    """The core name as a filename may carry it: a run of forbidden characters becomes one
    hyphen, which is the same shape ``normalize_entity_name`` gives a link target.

    Replacing with an underscore would not do: ``Concept_Foo_Bar.md`` is refused by the
    validator's strict pattern, so a space-bearing or underscored target would leave every
    stub unwritable.
    """
    return _FORBIDDEN_FILENAME_CHARS.sub("-", name).strip("-")


def stub_page_name(target: str) -> str | None:
    """The filename a stub for ``target`` must have, or ``None`` when it must not exist.

    ``None`` means "do not create a page" and has exactly one cause: the target's type is in
    :data:`GENERATED_NODE_TYPES`, whose pages the indexer skips.
    """
    target = (target or "").strip()
    if not target:
        return None
    declared = type_for_node_id(target)
    if stub_type(target) in GENERATED_NODE_TYPES:
        return None
    core = _sanitize_core(strip_prefix(target) if declared else target)
    if not core:
        return None
    # An untyped target needs the prefix: a bare ``<target>.md`` is not a valid node filename,
    # so the page would never be indexed and the link would never resolve.
    return f"{declared and prefix_for(declared) or 'Concept_'}{core}.md"


def stub_frontmatter(page_stem: str, node_type: str, today: str) -> dict:
    """The frontmatter a stub declares.  Every required field is present.

    ``title`` is the page's *core* name, not the stem: a stub for ``[[BrandNew-Thing]]`` is
    titled ``BrandNew-Thing``, not ``Concept_BrandNew-Thing``.  The live wiki's pages carry
    their bare name as the title, and the title is one of the routes by which a link
    resolves, so a prefixed title would register a name nothing ever links to.
    """
    stamp = f"{today}T00:00:00Z"
    return {
        "id": _generate_id(today),
        "title": strip_prefix(page_stem),
        "type": node_type,
        "domain": "General",
        "topic_cluster": "General",
        "status": "Active",
        "epistemic-status": "seed",
        "categories": ["Uncategorized"],
        "tags": [_STUB_MARKER_TAG],
        "sources": [],
        "strategic_scope": "edge",
        "evidence_tier": "derived",
        "created": stamp,
        "updated": stamp,
    }


def stub_body(page_stem: str, node_type: str, today: str) -> str:
    """The body: the two mandated H2 sections, and a first H3 slot if the type declares one.

    The heading is the core name and the self-link names the page that exists, so the one
    link a stub plants resolves.  The date is written bare, as the live wiki writes it
    (``Last Reshaped: 2026-06-02)``): a ``[[2026-06-02]]`` link would be a broken link today
    and a junk ``Concept_2026-06-02.md`` page the moment anybody runs lint ``--auto-fix``.

    Types without slots (``source``) get the fallback line, which validates because
    ``schema_validator`` only enforces slots for the types that declare them.  ``synthesis``
    is the exception: it demands two specific H2 sections this body does not have, so its
    stubs are still refused -- recorded in ``tests/test_lint_stub_type.py`` rather than left
    silent.
    """
    core = strip_prefix(page_stem)
    slots = VALID_H3_SLOTS.get(node_type) or [_FALLBACK_SLOT]
    return (
        f"\n# {core}\n\n"
        "## 1. 编译事实\n"
        "*[System Directive: This section represents the LATEST consensus.]*\n\n"
        f"Auto-generated stub for {core}. (Last Reshaped: {today})\n\n"
        f"{slots[0]}\n- [[{page_stem}]] Auto-generated stub.\n\n"
        "---\n\n"
        "## 2. 证据时间线\n"
        "*[System Directive: This is the immutable event ledger.]*\n\n"
        f"- [{today}] [Observation] Created stub.\n"
    )


def create_stub(
    wiki_dir: str,
    target: str,
    index: tuple[set[str], set[str], dict[str, str]] | None = None,
) -> str | None:
    """Write the stub ``target`` needs, unless something already covers it.

    Returns the stem written (``"Concept_BrandNew-Thing"`` -- the caller adds it to its own
    link maps) or ``None`` when nothing was written: generated type, already covered, or the
    write itself refused.  A refused write is logged and not raised, so one unwritable stub
    does not abandon the rest of a pass.
    """
    page_name = stub_page_name(target)
    if page_name is None:
        log.info(
            "Not creating a stub for '%s': %s pages are generated artifacts, not graph nodes.",
            target,
            type_for_node_id(target),
        )
        return None
    index = index if index is not None else existence_index(wiki_dir)
    stem = page_name[:-3]
    covering = covering_page(stem, index)
    if covering:
        if covering != stem:
            log.warning(
                "Not creating %s.md: %s already covers that name; fix the link instead.",
                stem,
                covering,
            )
        return None
    node_type = stub_type(stem)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    try:
        write_markdown_file(
            os.path.join(wiki_dir, page_name),
            stub_frontmatter(stem, node_type, today),
            stub_body(stem, node_type, today),
            skip_validation=False,
        )
    except Exception as exc:  # noqa: BLE001 - one refused stub must not stop the pass
        log.warning("Failed to create stub %s.md: %s", stem, exc)
        return None
    stems, normalized, cores = index
    stems.add(stem)
    normalized.add(normalize_entity_name(stem))
    cores[strip_prefix(stem)] = stem
    log.info("Created stub page: %s.md", stem)
    return stem
