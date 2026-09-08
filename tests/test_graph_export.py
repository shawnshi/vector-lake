"""Committed graph export, sandbox and offline renderer regression contract."""

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import anyio
import pytest

from vector_lake import db_store, governance_store, indexer, mcp_server, tool_graph
from vector_lake import projection_format_v2 as fmt
from vector_lake.heavy_task_gate import HeavyTaskBusy, heavy_task


@pytest.fixture
def committed_graph(isolated_memory, tmp_path, monkeypatch):
    db_store.init_db()
    wiki = isolated_memory / "wiki"
    index = {
        "nodes": {
            "Concept_One": {
                "title": "One",
                "links": ["Concept_Two"],
                "summary": "</script><img src=x onerror=alert(1)>",
            },
            "Concept_Two": {"title": "Two", "links": []},
        },
        "weighted_edges": [
            {"source": "Concept_One", "target": "Concept_Two", "weight": 1}
        ],
        "governance_metrics": {
            "validity_state_counts": {"unknown": "<img src=x onerror=alert(2)>"}
        },
    }
    claims = {"nodes": [{"id": "claim_one", "name": "Synthetic claim"}], "edges": []}
    prepared = fmt.build_projection_roots(
        wiki,
        index,
        claims,
        canonical_generation=fmt._runtime_current_generations(
            db_store.get_connection()
        ),
    )
    fmt.publish_prepared_projection(wiki, prepared)
    db_store.close_all_connections()
    output = tmp_path / "artifacts"
    monkeypatch.setenv("VECTOR_LAKE_AGENT_SANDBOX_ROOTS", str(output))
    monkeypatch.setattr(tool_graph.webbrowser, "open", lambda uri: False)
    return wiki, output, prepared


def _forbid_mutations(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("export attempted a canonical mutation/initialization")

    monkeypatch.setattr(governance_store, "ensure_canonical_store_populated", forbidden)
    monkeypatch.setattr(indexer, "generate_index", forbidden)
    monkeypatch.setattr(db_store, "get_connection", forbidden)
    monkeypatch.setattr(db_store, "init_db", forbidden)
    monkeypatch.setattr(db_store, "transaction", forbidden)


def _file_hashes(directory):
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in directory.rglob("*")
        if p.is_file() and not p.name.endswith(".lock")
    }


def test_graph_export_committed_pair_is_read_only_and_meaningful(
    committed_graph, monkeypatch
):
    wiki, output, prepared = committed_graph
    before = _file_hashes(wiki)
    _forbid_mutations(monkeypatch)
    result = tool_graph.visualize_vector_lake()
    assert result.startswith("Saved graph:"), result
    assert "Browser did not open" in result
    assert prepared.projection_generation in result
    html = (output / "vector_lake_graph.html").read_text(encoding="utf-8")
    assert "%%GRAPH_DATA%%" not in html and "%%MEMORY_BASE_PATH%%" not in html
    assert "</script><img" not in html
    match = re.search(r"const rawData = (.*);", html)
    assert match is not None
    payload = json.loads(match.group(1))
    assert len(payload["pageGraph"]["nodes"]) == 2
    assert len(payload["pageGraph"]["edges"]) == 1
    assert len(payload["claimGraph"]["nodes"]) == 1
    assert payload["pageGraph"]["nodes"][0]["summary"].startswith("</script>")
    metadata = payload["exportMetadata"]
    assert metadata["projection_generation"] == prepared.projection_generation
    assert (
        metadata["canonical_generation"]["runtime_generations"]
        == prepared.canonical_generation
    )
    assert metadata["page_nodes"] == 2 and metadata["claim_nodes"] == 1
    assert "2500" in metadata["claim_scope_warning"]
    assert "${escapeHTML(count)}" in html and "${count}</div>" not in html
    assert ".nodeLabel(node => escapeHTML(" in html
    assert _file_hashes(wiki) == before


@pytest.mark.parametrize(
    "damage",
    [
        "index_missing",
        "claim_missing",
        "sidecar_missing",
        "sidecar_corrupt",
        "object_corrupt",
        "stale",
    ],
)
def test_graph_export_projection_failure_has_no_artifact(
    committed_graph, monkeypatch, damage
):
    wiki, output, prepared = committed_graph
    if damage == "index_missing":
        (wiki / "index.json").unlink()
    elif damage == "claim_missing":
        (wiki / "claim_graph.json").unlink()
    elif damage == "sidecar_missing":
        (wiki / fmt.SIDECAR_FILENAME).unlink()
    elif damage == "sidecar_corrupt":
        (wiki / fmt.SIDECAR_FILENAME).write_text("{}", encoding="utf-8")
    elif damage == "object_corrupt":
        from vector_lake.projection_store_v2 import ProjectionStoreV2

        store = ProjectionStoreV2(wiki)
        store.object_path(prepared.index_root_sha256).write_bytes(b"corrupt")
    else:
        with db_store.transaction() as conn:
            conn.execute("UPDATE runtime_generations SET generation = generation + 1")
        db_store.close_all_connections()
    before = _file_hashes(wiki)
    _forbid_mutations(monkeypatch)
    result = tool_graph.visualize_vector_lake()
    assert result.startswith("Error:"), result
    assert not output.exists()
    assert _file_hashes(wiki) == before


@pytest.mark.parametrize("roots", [None, "", " ; " if os.name == "nt" else " : "])
@pytest.mark.parametrize("explicit", [False, True])
def test_graph_export_unconfigured_output_rejected_before_read(
    tmp_path, monkeypatch, roots, explicit
):
    if roots is None:
        monkeypatch.delenv("VECTOR_LAKE_AGENT_SANDBOX_ROOTS", raising=False)
    else:
        monkeypatch.setenv("VECTOR_LAKE_AGENT_SANDBOX_ROOTS", roots)
    monkeypatch.setattr(
        tool_graph,
        "_read_projection_pair",
        lambda *a: pytest.fail("read before authorization"),
    )
    output = tmp_path / "output"
    for call in (tool_graph.visualize_vector_lake, mcp_server.visualize_vector_lake):
        assert "VECTOR_LAKE_AGENT_SANDBOX_ROOTS" in call(
            str(output) if explicit else None
        )
    assert not output.exists()


@pytest.mark.parametrize(
    "kind",
    [
        "empty",
        "whitespace",
        "relative",
        "traversal",
        "outside",
        "file",
        "target_directory",
        "invalid_root",
    ],
)
def test_graph_output_rejects_unsafe_paths_without_side_effects(
    tmp_path, monkeypatch, kind
):
    root = tmp_path / "approved"
    root.mkdir()
    monkeypatch.setenv("VECTOR_LAKE_AGENT_SANDBOX_ROOTS", str(root))
    value = {
        "empty": "",
        "whitespace": " ",
        "relative": "relative",
        "traversal": str(root / ".." / "approved" / "new"),
        "outside": str(tmp_path / "outside"),
    }.get(kind, str(root))
    if kind == "file":
        value = str(root / "file")
        Path(value).write_text("untouched", encoding="utf-8")
    elif kind == "target_directory":
        (root / "vector_lake_graph.html").mkdir()
    elif kind == "invalid_root":
        monkeypatch.setenv("VECTOR_LAKE_AGENT_SANDBOX_ROOTS", "relative-root")
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    for call in (tool_graph.visualize_vector_lake, mcp_server.visualize_vector_lake):
        assert call(value).startswith("Error:")
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before


@pytest.mark.parametrize("kind", ["root", "ancestor", "target"])
def test_graph_output_rejects_symlinks(tmp_path, monkeypatch, kind):
    root = tmp_path / "approved"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    target = outside
    if kind == "target":
        link = root / "vector_lake_graph.html"
        target = outside / "untouched.html"
        target.write_text("untouched", encoding="utf-8")
    try:
        link.symlink_to(target, target_is_directory=kind != "target")
    except OSError:
        pytest.skip("Windows token cannot create symlinks")
    monkeypatch.setenv(
        "VECTOR_LAKE_AGENT_SANDBOX_ROOTS", str(link if kind == "root" else root)
    )
    output = str(link / "new") if kind == "ancestor" else None
    result = tool_graph.visualize_vector_lake(output)
    assert "symlink or reparse" in result
    assert list(outside.iterdir()) == ([target] if kind == "target" else [])


def test_graph_output_default_and_multiple_roots(tmp_path, monkeypatch):
    roots = [tmp_path / "first", tmp_path / "second"]
    monkeypatch.setenv(
        "VECTOR_LAKE_AGENT_SANDBOX_ROOTS", os.pathsep.join(map(str, roots))
    )
    assert tool_graph._graph_output_path() == roots[0] / "vector_lake_graph.html"
    assert (
        tool_graph._graph_output_path(str(roots[1] / "sub"))
        == roots[1] / "sub" / "vector_lake_graph.html"
    )
    assert not any(root.exists() for root in roots)


def test_graph_export_browser_exception_does_not_hide_saved_file(
    committed_graph, monkeypatch
):
    _, output, _ = committed_graph

    def fail(uri):
        raise RuntimeError("browser unavailable")

    monkeypatch.setattr(tool_graph.webbrowser, "open", fail)
    result = tool_graph.visualize_vector_lake()
    assert result.startswith("Saved graph:") and "Browser launch failed" in result
    assert (output / "vector_lake_graph.html").is_file()


def test_graph_export_atomic_failure_preserves_prior_file(committed_graph, monkeypatch):
    _, output, _ = committed_graph
    output.mkdir()
    target = output / "vector_lake_graph.html"
    target.write_text("prior export", encoding="utf-8")

    def fail(*args):
        raise OSError("injected publish failure")

    monkeypatch.setattr(tool_graph.os, "replace", fail)
    result = tool_graph.visualize_vector_lake()
    assert "not published" in result
    assert target.read_text(encoding="utf-8") == "prior export"
    assert list(output.iterdir()) == [target]


def test_graph_export_uses_heavy_executor_without_canonical_gate(
    committed_graph, tmp_path, monkeypatch
):
    wiki, output, _ = committed_graph
    monkeypatch.setenv("VECTOR_LAKE_MCP_HEAVY_TASK_WAIT_SECONDS", "0.01")
    monkeypatch.setenv("VECTOR_LAKE_MCP_SURFACE", "full")
    guard = mcp_server.MCPRuntimeGuard(tmp_path, check_interval_seconds=60)
    server = mcp_server.ReloadAwareFastMCP("graph-export-test", runtime_guard=guard)
    server.tool()(mcp_server.visualize_vector_lake)

    @server.tool()
    def projection_report() -> str:
        pytest.fail("unrelated scan bypassed gate")

    @server.tool()
    def projection_rebuild_index() -> str:
        pytest.fail("mutation bypassed gate")

    try:
        with heavy_task(
            "maintenance", "unrelated-owner", origin="pytest", wait_timeout_seconds=0
        ):
            before = _file_hashes(wiki)
            _forbid_mutations(monkeypatch)
            registered = server._tool_manager.get_tool("visualize_vector_lake")
            assert registered is not None
            result = anyio.run(registered.fn)
            assert result.startswith("Saved graph:"), result
            assert _file_hashes(wiki) == before
            for name in ("projection_report", "projection_rebuild_index"):
                registered = server._tool_manager.get_tool(name)
                assert registered is not None
                with pytest.raises(HeavyTaskBusy):
                    anyio.run(registered.fn)
        status = server.blocking_executor_status()
        assert status["heavy_lane"]["metrics"]["submitted"] == 3
        assert status["fast_lane"]["metrics"]["submitted"] == 0
        assert (output / "vector_lake_graph.html").is_file()
    finally:
        server.shutdown_blocking_executor(wait=True, timeout=2)


def test_graph_script_json_escapes_paths_and_payload():
    value = '"</script>&' + chr(0x2028) + chr(0x2029)
    encoded = tool_graph._script_json(value)
    assert "<" not in encoded and ">" not in encoded and "&" not in encoded
    assert chr(0x2028) not in encoded and chr(0x2029) not in encoded
    assert json.loads(encoded) == value


def test_graph_renderer_no_network_until_consent(tmp_path):
    html = (
        Path(__file__).resolve().parents[1] / "templates" / "topology.html"
    ).read_text(encoding="utf-8")
    assert not re.search(
        r"<(?:script|img|iframe|link)[^>]+(?:src|href)\s*=", html, re.I
    )
    assert "@import" not in html and "url(" not in html
    assert "External scripts can access all graph data" in html
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable for isolated JavaScript behavior test")
    match = re.search(r"<script>(.*?)</script>", html, re.S)
    assert match is not None
    script = match.group(1)
    script = script.replace(
        "%%GRAPH_DATA%%", '{"exportMetadata":{"projection_generation":"synthetic"}}'
    ).replace("%%MEMORY_BASE_PATH%%", '"file:///synthetic/"')
    harness = """
const vm = require('node:vm');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const elements = new Map();
let appended = [];
let created = [];
const document = {
  getElementById(id) { if (!elements.has(id)) elements.set(id, {style:{}}); return elements.get(id); },
  createElement(tag) { created.push(tag); return {}; },
  head: {appendChild(element) { appended.push(element); }}
};
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {document});
assert.equal(created.length, 0);
assert.equal(appended.length, 0);
assert.match(elements.get('export-metadata').textContent, /synthetic/);
elements.get('load-renderer').onclick();
assert.deepEqual(created, ['script']);
assert.equal(appended.length, 1);
assert.equal(appended[0].src, 'https://unpkg.com/3d-force-graph');
appended[0].onerror();
assert.match(elements.get('renderer-status').textContent, /remains saved/);
"""
    js = tmp_path / "graph.js"
    js.write_text(script, encoding="utf-8")
    runner = tmp_path / "offline.cjs"
    runner.write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [node, str(runner), str(js)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_graph_export_missing_database_never_initializes(committed_graph, monkeypatch):
    wiki, output, _ = committed_graph
    database = db_store.peek_db_path()
    database.unlink()
    _forbid_mutations(monkeypatch)
    result = tool_graph.visualize_vector_lake()
    assert "database_missing" in result
    assert not database.exists() and not output.exists()


def test_graph_export_read_connection_is_query_only(committed_graph, monkeypatch):
    import sqlite3

    original = tool_graph.load_committed_pair
    observed = []

    def verify(base, *, connection):
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE forbidden_graph_write (id INTEGER)")
        observed.append(True)
        return original(base, connection=connection)

    monkeypatch.setattr(tool_graph, "load_committed_pair", verify)
    assert tool_graph.visualize_vector_lake().startswith("Saved graph:")
    assert observed == [True]


def test_graph_export_live_wal_is_read_only(committed_graph, monkeypatch):
    wiki, _, _ = committed_graph
    with db_store.transaction() as conn:
        conn.execute("CREATE TABLE graph_export_wal_fixture (value TEXT)")
        conn.execute("INSERT INTO graph_export_wal_fixture VALUES ('synthetic')")
    wal = db_store.peek_db_path().with_name("vector_lake.db-wal")
    assert wal.is_file() and wal.stat().st_size > 0
    before = {
        name: digest
        for name, digest in _file_hashes(wiki).items()
        if not name.endswith("-shm")
    }
    _forbid_mutations(monkeypatch)
    result = tool_graph.visualize_vector_lake()
    assert result.startswith("Saved graph:"), result
    after = {
        name: digest
        for name, digest in _file_hashes(wiki).items()
        if not name.endswith("-shm")
    }
    assert after == before


def test_graph_export_cancellation_before_publish_preserves_prior(
    committed_graph, monkeypatch
):
    from vector_lake.cancellation import (
        CancellationRegistry,
        CooperativeCancellation,
        bind_cancellation_operation,
    )

    _, output, _ = committed_graph
    output.mkdir()
    target = output / "vector_lake_graph.html"
    target.write_text("prior", encoding="utf-8")
    operation = CancellationRegistry().create(
        tool_name="visualize_vector_lake", lane="heavy", deadline=None
    )
    operation.mark_running()
    original = tool_graph._graph_output_path
    checks = []

    def cancel_before_atomic(value=None):
        path = original(value)
        checks.append(path)
        if len(checks) == 3:
            operation.request_cancellation("test", detached=False)
        return path

    monkeypatch.setattr(tool_graph, "_graph_output_path", cancel_before_atomic)
    with bind_cancellation_operation(operation), pytest.raises(CooperativeCancellation):
        tool_graph.visualize_vector_lake()
    assert target.read_text(encoding="utf-8") == "prior"
    assert list(output.iterdir()) == [target]


def test_graph_output_rejects_windows_reparse_attribute(tmp_path, monkeypatch):
    from types import SimpleNamespace

    root = tmp_path / "approved"
    root.mkdir()
    original = Path.lstat

    def reparse(path, *args, **kwargs):
        details = original(path, *args, **kwargs)
        if path == root:
            return SimpleNamespace(st_mode=details.st_mode, st_file_attributes=0x400)
        return details

    monkeypatch.setenv("VECTOR_LAKE_AGENT_SANDBOX_ROOTS", str(root))
    monkeypatch.setattr(Path, "lstat", reparse)
    assert "reparse" in tool_graph.visualize_vector_lake()
    assert list(root.iterdir()) == []


_GRAPH_INITIALIZED_HARNESS = r"""
const vm = require('node:vm');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const fixture = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const elements = new Map();
const appended = [];
function element() {
    const classes = new Set();
    return {
        style: {}, children: [], attributes: {},
        setAttribute(name, value) { this.attributes[name] = value; },
        focus() { this.focused = true; },
        classList: {
            add(value) { classes.add(value); },
            remove(value) { classes.delete(value); },
            contains(value) { return classes.has(value); },
            toggle(value, force) {
                if (force === true || (force === undefined && !classes.has(value))) classes.add(value);
                else classes.delete(value);
            }
        },
        set innerHTML(value) { this.children = []; this.html = value; },
        get innerHTML() { return this.html || ''; },
        appendChild(child) { this.children.push(child); },
        replaceChildren(...children) { this.children = children; },
        querySelector() { return this.input || (this.input = element()); },
        addEventListener(name, handler) { this['on' + name] = handler; }
    };
}
const document = {
    getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
    createElement() { return element(); },
    head: {appendChild(script) { appended.push(script); }},
    body: element()
};
const settings = new Map();
const setCounts = new Map();
let graph;
graph = new Proxy({}, {get(target, name) {
    if (name === 'd3Force') return () => ({strength() {}, distance() {}});
    return (...args) => {
        if (!args.length && name !== 'd3ReheatSimulation') return settings.get(name);
        if (name === 'nodeOpacity') assert.equal(typeof args[0], 'number', 'official API requires global numeric opacity');
        settings.set(name, args[0]);
        setCounts.set(name, (setCounts.get(name) || 0) + 1);
        return graph;
    };
}});
const context = vm.createContext({document, URL, ForceGraph3D: () => () => graph, setTimeout: fn => fn()});
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
assert.equal(appended.length, 0, 'initial page must not request the CDN');
document.getElementById('load-renderer').onclick();
assert.equal(appended.length, 1);
appended[0].onload(); // Synthetic renderer only: never fetch the script.
assert.equal(document.getElementById('renderer-consent').style.display, 'none',
    document.getElementById('renderer-status').textContent);
assert.equal(document.getElementById('loading').style.display, 'none');
const nodes = settings.get('graphData').nodes;
"""


def _run_initialized_graph_check(
    tmp_path, assertions, root_name="Graph Memory", payload_override=None
):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable for isolated initialized-renderer test")
    memory = tmp_path / root_name
    names = ["Graph Memory", "中文", "literal%", "literal%20"]
    page_nodes = [
        {
            "id": "Concept_" + name,
            "name": name,
            "group": "Concept",
            "sources": ["raw/" + name + "/note " + name + ".md"],
        }
        for name in names
    ]
    claim_nodes = [
        {
            "id": "claim_" + name,
            "name": name,
            "group": "Claim",
            "sources": ["Source_" + name + ".md"],
        }
        for name in names
    ]
    payload = {
        "pageGraph": {
            "nodes": page_nodes,
            "edges": [{"source": page_nodes[1]["id"], "target": page_nodes[2]["id"]}],
        },
        "claimGraph": {"nodes": claim_nodes, "edges": []},
        "exportMetadata": {"projection_generation": "synthetic"},
    }
    if payload_override is not None:
        payload = payload_override
    fixture = {
        "pageHrefs": [
            (memory / "wiki" / (n["id"] + ".md")).as_uri() for n in page_nodes
        ],
        "pageSourceHrefs": [(memory / n["sources"][0]).as_uri() for n in page_nodes],
        "claimSourceHrefs": [
            (memory / "wiki" / n["sources"][0]).as_uri() for n in claim_nodes
        ],
    }
    html = (
        Path(__file__).resolve().parents[1] / "templates" / "topology.html"
    ).read_text(encoding="utf-8")
    match = re.search(r"<script>(.*?)</script>", html, re.S)
    assert match is not None
    script = (
        match.group(1)
        .replace("%%GRAPH_DATA%%", tool_graph._script_json(payload))
        .replace("%%MEMORY_BASE_PATH%%", tool_graph._script_json(memory.as_uri() + "/"))
    )
    # Emulate existing inline handler too, so this test reproduces the old scope bug.
    inline = re.search(r'id="close-btn" onclick="([^"]*)"', html)
    if inline:
        script += (
            "document.getElementById('close-btn').onclick = function() {"
            + inline.group(1)
            + "};"
        )
    script_path = tmp_path / "initialized-graph.js"
    script_path.write_text(script, encoding="utf-8")
    fixture_path = tmp_path / "hrefs.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    runner = tmp_path / "initialized-graph.cjs"
    runner.write_text(_GRAPH_INITIALIZED_HARNESS + assertions, encoding="utf-8")
    result = subprocess.run(
        [node, str(runner), str(script_path), str(fixture_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "root_name", ["Graph Memory", "中文根", "literal%", "literal%20"]
)
def test_graph_initialized_exact_file_hrefs(tmp_path, root_name):
    _run_initialized_graph_check(
        tmp_path,
        r"""
for (let i = 0; i < nodes.length; i++) {
    settings.get('onNodeClick')(nodes[i]);
    assert.equal(document.getElementById('info-title').children[0].href, fixture.pageHrefs[i]);
    assert.equal(document.getElementById('info-sources').children[0].children[0].href, fixture.pageSourceHrefs[i]);
}
document.getElementById('btn-claim-view').onclick();
const claims = settings.get('graphData').nodes;
for (let i = 0; i < claims.length; i++) {
    settings.get('onNodeClick')(claims[i]);
    assert.equal(document.getElementById('info-title').children.length, 0);
    assert.equal(document.getElementById('info-sources').children[0].children[0].href, fixture.claimSourceHrefs[i]);
}
assert.equal(vm.runInContext("sanitizeURL('https://example.invalid/a%20b')", context), 'https://example.invalid/a%20b');
for (const value of ['javascript:alert(1)', 'java	script:alert(1)', 'data:text/html,unsafe', 'vbscript:unsafe']) {
    context.unsafeURL = value;
    assert.equal(vm.runInContext('sanitizeURL(unsafeURL)', context), null);
}
""",
        root_name,
    )


def test_graph_initialized_close_and_background_clear_selection(tmp_path):
    _run_initialized_graph_check(
        tmp_path,
        r"""
const other = nodes[3];
const edge = settings.get('graphData').links[0];
const baseline = () => [settings.get('nodeColor')(other), settings.get('nodeOpacity'),
    settings.get('linkVisibility')(edge), settings.get('linkColor')(edge)];
const initial = baseline();
for (const action of ['close', 'background']) {
    settings.get('onNodeClick')(nodes[0]);
    assert.equal(document.getElementById('info-panel').classList.contains('visible'), true);
    assert.notDeepEqual(baseline(), initial);
    assert.equal(settings.get('linkVisibility')(edge), false);
    const refreshes = setCounts.get('nodeColor');
    if (action === 'close') document.getElementById('close-btn').onclick();
    else settings.get('onBackgroundClick')();
    assert.equal(document.getElementById('info-panel').classList.contains('visible'), false);
    assert.deepEqual(baseline(), initial, action + ' must clear closure-local selection');
    assert.ok(setCounts.get('nodeColor') > refreshes, action + ' must refresh renderer');
}
assert.equal(Object.hasOwn(context, 'selectedNode'), false, 'no global selection shadow');
""",
    )
