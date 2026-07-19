from collections import defaultdict

from vector_lake.tool_lint import _register_link_target, _resolve_link_target
from vector_lake.wiki_utils import iter_wiki_link_matches, markdown_fenced_code_spans


def test_temporal_link_is_not_a_missing_page_target():
    assert _resolve_link_target("2026-07-13", {}, defaultdict(set)) is None


def test_link_prefix_variation_resolves_only_unique_target():
    exact = {}
    normalized = defaultdict(set)
    _register_link_target(exact, normalized, "Institution_梅奥诊所", "Institution_梅奥诊所")

    assert (
        _resolve_link_target("Vendor_梅奥诊所", exact, normalized)
        == "Institution_梅奥诊所"
    )


def test_ambiguous_normalized_link_remains_unresolved():
    exact = {}
    normalized = defaultdict(set)
    _register_link_target(exact, normalized, "Concept_Meta", "Concept_Meta")
    _register_link_target(exact, normalized, "Vendor_Meta", "Vendor_Meta")

    assert _resolve_link_target("Meta", exact, normalized) is None


def test_link_iterator_excludes_fenced_and_inline_code():
    content = """[[Visible]]

`[[Inline-Code]]`

```markdown
[[Fenced-Code]]
> ```text
> [[Nested-Literal]]
> ```
```
"""

    targets = [match.group(1) for match in iter_wiki_link_matches(content)]

    assert targets == ["Visible"]
    spans = markdown_fenced_code_spans(content)
    assert len(spans) == 1
    assert "[[Fenced-Code]]" in content[spans[0][0] : spans[0][1]]
