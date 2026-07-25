import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "templates" / "topology.html"


def _sanitize_url_function() -> str:
    content = TEMPLATE.read_text(encoding="utf-8")
    prefix = "        function sanitizeURL(url) {"
    start = content.index(prefix)
    end = content.index("\n\n        const rawData", start)
    return content[start:end].strip()


def test_sanitize_url_allows_supported_locations_and_blocks_script_schemes():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to execute the topology URL sanitizer")
    cases = [
        {"value": "https://example.com/a b", "expected": "https://example.com/a%20b"},
        {"value": "HTTP://example.com/a", "expected": "HTTP://example.com/a"},
        {"value": "file:///C:/Vector Lake/Page.md", "expected": "file:///C:/Vector%20Lake/Page.md"},
        {"value": "/wiki/Page Name.md", "expected": "/wiki/Page%20Name.md"},
        {"value": "#node id", "expected": "#node%20id"},
        {"value": "wiki/Page Name.md", "expected": "wiki/Page%20Name.md"},
        {"value": "javascript:alert(1)", "expected": None},
        {"value": " JaVaScRiPt:alert(1)", "expected": None},
        {"value": "data:text/html,<script>alert(1)</script>", "expected": None},
        {"value": "vbscript:msgbox(1)", "expected": None},
        {"value": "", "expected": None},
        {"value": None, "expected": None},
    ]
    script = (
        _sanitize_url_function()
        + "\nconst cases = "
        + json.dumps(cases)
        + ";\nconsole.log(JSON.stringify(cases.map(item => sanitizeURL(item.value))));"
    )

    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert json.loads(completed.stdout) == [item["expected"] for item in cases]


def test_topology_builds_external_links_without_inner_html():
    content = TEMPLATE.read_text(encoding="utf-8")

    assert "document.getElementById('info-title').innerHTML" not in content
    assert "titleElement.replaceChildren()" in content
    assert "titleLink.textContent = titleText" in content
    assert "titleLink.href = safeHref" in content
    assert "titleLink.rel = 'noopener noreferrer'" in content
    assert "const safeSourceHref = sanitizeURL(hrefValue)" in content
    assert "a.href = safeSourceHref" in content
    assert "a.rel = 'noopener noreferrer'" in content
