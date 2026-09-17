"""The "is this file a knowledge node at all?" set has one owner.

``NON_NODE_WIKI_FILES`` had been spelled out six times -- in ``wiki_utils``,
``schema_validator``, ``indexer``, ``runtime_health``, ``tool_doctor`` and
``tool_projection`` -- as an inline literal, a tuple or a module constant.  The values
happened to agree, so nothing was visibly broken; what the duplication bought was that
the next edit would land in whichever of the six someone happened to be looking at.

Two of those six were not carrying information even as written.  ``runtime_health`` and
``tool_doctor`` both passed a local copy of the set to ``wiki_page_keys``, whose default
parameter was already exactly that set, so they replaced a default with a copy of it.

The guard counts the members that appear as string constants in each file rather than
scanning text, so quote style does not hide a copy and a tuple-and-literal pair is not
counted twice.  It fires at four members and not three, because seven other call sites
hold a genuinely narrower ``{index.md, log.md, overview.md}`` list -- which files to read
or delete -- and that question has its own answer; see the owner's comment.  It cannot
see a set assembled from variables, or a member list that lives outside
``vector_lake/*.py``.
"""

import ast
import pathlib

import pytest

from vector_lake import schema_validator, wiki_utils
from vector_lake.node_vocabulary import NON_NODE_WIKI_FILES

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "vector_lake"
OWNER = PACKAGE / "node_vocabulary.py"

#: A file holding more of these than the narrower three-file list can legitimately
#: hold is spelling the node set out again.
REDECLARATION_THRESHOLD = 4


def _members_present(source: str) -> set[str]:
    """Members of the set that appear as string constants anywhere in ``source``."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in NON_NODE_WIKI_FILES:
                found.add(node.value)
    return found


def test_wiki_utils_reexports_the_same_object():
    """Not an equal set -- the same object, so the two names cannot drift apart."""
    assert wiki_utils.SYSTEM_WHITELIST is NON_NODE_WIKI_FILES


def test_the_set_holds_exactly_the_six_meta_files():
    assert NON_NODE_WIKI_FILES == {
        "index.md",
        "log.md",
        "overview.md",
        "orphan_pages.md",
        "wiki_link_stats.md",
        "Synthesis_log.md",
    }


@pytest.mark.parametrize("filename", sorted(NON_NODE_WIKI_FILES))
def test_the_schema_validator_accepts_a_meta_file_as_a_non_node(filename):
    """The shared set is what the validator actually consults, not a parallel copy."""
    schema_validator.validate_schema(
        {}, "this body is never read for a meta file", filename
    )


def test_only_the_owner_spells_the_set_out():
    offenders = {}
    for path in sorted(PACKAGE.glob("*.py")):
        if path == OWNER:
            continue
        present = _members_present(path.read_text(encoding="utf-8"))
        if len(present) >= REDECLARATION_THRESHOLD:
            offenders[path.name] = sorted(present)
    assert not offenders, (
        f"{offenders}: import NON_NODE_WIKI_FILES from vector_lake.node_vocabulary instead "
        f"of re-listing it. {REDECLARATION_THRESHOLD} or more members is more than the "
        "narrower {index.md, log.md, overview.md} list any call site needs."
    )


def test_the_guard_fires_on_a_redeclared_set():
    """The guard has to be able to fail, or it asserts nothing about the package."""
    source = (
        'EXCLUDED = {"index.md", "log.md", "overview.md", "orphan_pages.md"}\n'
    )
    assert len(_members_present(source)) >= REDECLARATION_THRESHOLD


def test_the_guard_tolerates_the_narrower_three_file_list():
    """The seven sites holding this list are not the defect this guard is about."""
    source = 'PERMITTED = {"index.md", "log.md", "overview.md"}\n'
    assert len(_members_present(source)) < REDECLARATION_THRESHOLD
