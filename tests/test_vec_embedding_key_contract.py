"""``vec_embeddings.page_key`` holds a page key, and nothing may join it as an entity id.

The column held a page key (``Concept_...``) from the start, but was *named* ``entity_id`` until
2026-09-22 -- the same name as ``entities.entity_id``, a different identifier (``entity_<hex>``).
Measured on the live lake before the rename: five sampled rows matched
``page_index_nodes.node_key`` 5/5 and ``entities.entity_id`` 0/5.  The danger was never a live bug;
it was the join nobody had written yet, one that reads the shared name, returns nothing, and raises
no error.

vec0 refuses both RENAME COLUMN and ADD COLUMN, so the fix was a staged rebuild -- rows out to a
plain table, drop, recreate under the same name with ``page_key``, rows back, all in one
transaction.  This file now guards two things instead of one: that no statement joins the vector
column to the entity identifier, and that the old column name does not come back -- a rename that
can silently un-happen is not a rename.
"""

from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: An equality that would silently join the two namespaces.  Prose mentioning ``entities.entity_id``
#: next to ``vec_embeddings`` is fine -- the schema comment does exactly that on purpose -- so the
#: pattern looks for the join itself, not for co-occurrence.
CROSS_NAMESPACE_JOIN = re.compile(
    r"(entities\.entity_id\s*=\s*vec_embeddings\.page_key"
    r"|vec_embeddings\.page_key\s*=\s*entities\.entity_id"
    r"|vec_embeddings\.page_key\s*=\s*entities\b)"
)

#: The name this column used to have.  Reintroducing it anywhere in a statement that touches the
#: vector table would undo the rename for its readers while the schema kept the new name.
OLD_COLUMN_NAME = re.compile(r"vec_embeddings\.entity_id|entity_id\s+TEXT PRIMARY KEY")


def _string_literals(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def sql_sources() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for path in sorted((ROOT / "vector_lake").glob("*.py")) + sorted((ROOT / "scripts").glob("*.py")):
        for literal in _string_literals(path):
            if "vec_embeddings" in literal:
                out.append((path.name, literal))
    return out


def test_the_scan_finds_the_statements_it_is_supposed_to_check():
    """A guard that passes because it found nothing to compare is worse than no guard."""
    names = {name for name, _ in sql_sources()}
    assert {"db_store.py", "tool_search.py"} <= names, names


def test_no_statement_joins_the_vector_column_to_the_entity_identifier():
    offenders = [
        f"{name}: {CROSS_NAMESPACE_JOIN.search(literal).group(0)}"
        for name, literal in sql_sources()
        if CROSS_NAMESPACE_JOIN.search(literal)
    ]
    assert not offenders, (
        "vec_embeddings.page_key is a page key; joining it to entities.entity_id returns nothing "
        "and reports no error: " + "; ".join(offenders)
    )


def test_the_old_column_name_does_not_come_back():
    """The rename has to be able to fail loudly if it is undone, or it is only a comment."""
    offenders = [
        f"{name}: {OLD_COLUMN_NAME.search(literal).group(0)}"
        for name, literal in sql_sources()
        if OLD_COLUMN_NAME.search(literal)
    ]
    assert not offenders, (
        "the vector table's column is page_key; an entity_id column there would repeat the trap the "
        "rename removed: " + "; ".join(offenders)
    )


def test_the_pattern_fires_on_the_join_it_describes():
    """Synthetic input, so the assertion above cannot pass by matching nothing ever."""
    assert CROSS_NAMESPACE_JOIN.search("SELECT 1 FROM vec_embeddings JOIN entities ON entities.entity_id = vec_embeddings.page_key")
    assert CROSS_NAMESPACE_JOIN.search("SELECT 1 WHERE vec_embeddings.page_key = entities.entity_id")
    # Prose is not a join.
    assert not CROSS_NAMESPACE_JOIN.search("It is not entities.entity_id; see vec_embeddings.")
