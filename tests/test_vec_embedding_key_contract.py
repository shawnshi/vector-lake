"""``vec_embeddings.entity_id`` holds a page key, and nothing may join it as an entity id.

The column is named ``entity_id`` and the neighbouring table has a column of the same name holding
a *different* identifier (``entities.entity_id`` is ``entity_<hex>``; the vector row's value is
``Concept_...``).  Measured on the live lake: five sampled rows matched
``page_index_nodes.node_key`` 5/5 and ``entities.entity_id`` 0/5.  Every current caller passes a page
key -- ``db_store.delete_embedding`` deletes by ``node_key``, and ``tool_search`` fuses the result
with FTS rows that are keyed by ``node_key`` -- so retrieval is correct today.

What this guards is the join nobody has written yet: one that reads the shared column name and
returns nothing, with no error to notice.  vec0 refuses ``ALTER TABLE`` (both RENAME COLUMN and ADD
COLUMN), so the rename is a staged rebuild rather than a fix that can be applied on the spot; until
it happens, the name has to be checked.
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
    r"(entities\.entity_id\s*=\s*vec_embeddings\.entity_id"
    r"|vec_embeddings\.entity_id\s*=\s*entities\.entity_id"
    r"|vec_embeddings\.entity_id\s*=\s*entities\b)"
)


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
        "vec_embeddings.entity_id is a page key; joining it to entities.entity_id returns nothing "
        "and reports no error: " + "; ".join(offenders)
    )


def test_the_pattern_fires_on_the_join_it_describes():
    """Synthetic input, so the assertion above cannot pass by matching nothing ever."""
    assert CROSS_NAMESPACE_JOIN.search("SELECT 1 FROM vec_embeddings JOIN entities ON entities.entity_id = vec_embeddings.entity_id")
    assert CROSS_NAMESPACE_JOIN.search("SELECT 1 WHERE vec_embeddings.entity_id = entities.entity_id")
    # Prose is not a join.
    assert not CROSS_NAMESPACE_JOIN.search("It is not entities.entity_id; see vec_embeddings.")
