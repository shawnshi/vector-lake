"""Synthetic topology UI contracts; mock checks do not establish WebGL rendering."""

from tests.test_graph_export import _run_initialized_graph_check


def topology_payload():
  nodes: list[dict] = [
    {"id": key, "name": key, "group": "Concept", "raw_type": "Concept"}
    for key in ["root", "a", "b", "c", "d", "e", "f", "孤立"]
  ]
  nodes[0].update(
    name="根 <img onerror='boom'>",
    summary="*[System Directive: NO marketing fluff.]*\n# 标题\n**临床证据** [资料](https://example.invalid)\n"
    + "有意义的摘录。" * 60,
    semantic_links=["a"],
    sources=["raw/中文%目录/证据 #1.md"],
  )
  nodes[2]["group"] = "Hidden"
  edges = [{"source": "root", "target": key, "weight": 1} for key in "abcdef"]
  edges += [
    {"source": "a", "target": "root", "weight": 3},
    {"source": "root", "target": "a", "weight": 2},
    {"source": "root", "target": "root", "weight": 100},
    {"source": "root", "target": "missing", "weight": 100},
  ]
  return {
    "pageGraph": {"nodes": nodes, "edges": edges},
    "claimGraph": {"nodes": [{"id": "claim", "group": "Claim"}], "edges": []},
    "exportMetadata": {"projection_generation": "synthetic-only"},
  }


def run_check(tmp_path, assertions):
  _run_initialized_graph_check(
    tmp_path, assertions, payload_override=topology_payload()
  )


def test_graph_related_wiki_ranking_is_symmetric_finite_and_deterministic(tmp_path):
  run_check(
    tmp_path,
    r"""
const result = vm.runInContext(`(() => {
    const g = JSON.parse(JSON.stringify(rawData.pageGraph));
    for (const weight of [NaN, Infinity, -Infinity, '99', null, undefined]) {
        g.edges.push({source:'root', target:'b', weight});
    }
    g.edges.push({source:'root', target:'孤立', weight:NaN});
    const before = JSON.stringify(g);
    const ranked = relatedWiki(g, 'root').map(({node, weight}) => [node.id, weight]);
    assert.equal(JSON.stringify(g), before);
    assert.deepEqual(relatedWiki(g, 'missing'), []);
    assert.deepEqual(relatedWiki(g, '孤立'), []);
    assert.equal(relatedWiki(g, 'a')[0].node.id, 'root');
    assert.equal(relatedWiki(g, 'a')[0].weight, 3);
    g.edges.reverse();
    assert.equal(JSON.stringify(ranked), JSON.stringify(relatedWiki(g, 'root').map(({node, weight}) => [node.id, weight])));
    return ranked;
})()`, Object.assign(context, {assert}));
assert.equal(JSON.stringify(result), JSON.stringify([['a',3],['b',1],['c',1],['d',1],['e',1],['f',1]]));
""",
  )


def test_graph_related_wiki_five_more_back_reveal_and_empty(tmp_path):
  run_check(
    tmp_path,
    r"""
const el = id => document.getElementById(id);
settings.get('onNodeClick')(nodes[0]);
assert.equal(el('info-related').children.length, 5);
assert.equal(el('related-more').hidden, false);
el('related-more').onclick();
assert.equal(el('info-related').children.length, 6);
assert.equal(el('related-more').attributes['aria-expanded'], 'true');
el('related-more').onclick();
assert.equal(el('info-related').children.length, 5);
const a = el('info-related').children[0];
assert.match(a.children[1].href, /\/wiki\/a.md$/);
assert.equal(a.children[1].target, '_blank');
assert.equal(a.children[1].rel, 'noopener noreferrer');
a.children[0].onclick();
assert.equal(el('info-title').children[0].textContent, 'a');
assert.equal(el('btn-back').disabled, false);
el('btn-back').onclick();
assert.equal(el('info-related').children.length, 5);
assert.equal(el('btn-back').disabled, true);
const filter = el('type-filters').children.find(label => label.children[0].value === 'Hidden').children[0];
filter.onchange({target:{checked:false}});
assert.equal(settings.get('nodeVisibility')(nodes[2]), false);
assert.match(el('stats-panel').innerHTML, /7 \/ 8/);
assert.match(el('stats-panel').innerHTML, /8 \/ 10/); // invalid endpoint remains only in exported total
el('info-related').children[1].children[0].onclick();
assert.equal(el('navigation-prompt').hidden, false);
assert.equal(settings.get('nodeVisibility')(nodes[2]), false, 'no silent reveal');
assert.equal(el('info-title').children[0].textContent, nodes[0].name);
el('navigation-prompt').children[2].onclick();
assert.equal(el('navigation-prompt').hidden, true);
el('info-related').children[1].children[0].onclick();
el('navigation-prompt').children[1].onclick();
assert.equal(settings.get('nodeVisibility')(nodes[2]), true);
assert.equal(el('info-title').children[0].textContent, 'b');
el('btn-back').onclick();
assert.equal(el('info-title').children[0].textContent, nodes[0].name);
settings.get('onNodeClick')(nodes[7]);
assert.match(el('info-related').textContent, /暂无关联/);
assert.equal(el('related-more').hidden, true);
""",
  )


def test_graph_selection_search_hover_and_mode_reset(tmp_path):
  run_check(
    tmp_path,
    r"""
const el = id => document.getElementById(id);
const input = el('search-input');
input.value = 'root';
input.oninput({target:input});
assert.equal(settings.get('nodeColor')(nodes[1]), '#374151');
el('search-results').children[0].onclick();
assert.equal(input.value, 'root');
assert.notEqual(settings.get('nodeColor')(nodes[1]), '#374151', 'selection clears search emphasis');
const colors = nodes.map(settings.get('nodeColor'));
const links = settings.get('graphData').links.map(settings.get('linkVisibility'));
settings.get('onNodeHover')(nodes[7]);
assert.deepEqual(nodes.map(settings.get('nodeColor')), colors);
assert.deepEqual(settings.get('graphData').links.map(settings.get('linkVisibility')), links);
el('btn-focus-mode').onclick();
assert.equal(settings.get('nodeVisibility')(nodes[7]), false);
assert.equal(settings.get('nodeVisibility')(nodes[1]), true);
assert.match(el('stats-panel').innerHTML, /7 \/ 8/);
el('btn-claim-view').onclick();
assert.equal(el('info-panel').classList.contains('visible'), false);
assert.equal(el('related-section').hidden, true);
assert.equal(input.value, '');
assert.equal(el('search-results').children.length, 0);
assert.equal(el('btn-back').disabled, true);
const claim = settings.get('graphData').nodes[0];
assert.equal(settings.get('nodeVisibility')(claim), true);
settings.get('onNodeClick')(claim);
assert.equal(el('related-section').hidden, true);
el('btn-page-view').onclick();
assert.equal(el('info-panel').classList.contains('visible'), false);
assert.equal(settings.get('nodeVisibility')(nodes[7]), true);
assert.notEqual(settings.get('nodeColor')(nodes[1]), '#374151');
""",
  )


def test_graph_visual_changes_preserve_live_objects_and_freeze_restores_pins(tmp_path):
  run_check(
    tmp_path,
    r"""
const el = id => document.getElementById(id);
const data = settings.get('graphData');
nodes.forEach((n, i) => Object.assign(n, {x: i + 1, y: i + 2, z: i + 3}));
nodes[0].fx = 19; // preexisting fixed constraints must be restored
const coordinates = JSON.stringify(nodes);
const initialSets = setCounts.get('graphData');
el('btn-community').onclick();
el('btn-type').onclick();
el('type-filters').children[1].children[0].onchange({target:{checked:false}});
assert.equal(setCounts.get('graphData'), initialSets);
assert.equal(settings.get('graphData'), data);
assert.equal(JSON.stringify(nodes), coordinates);
assert.equal(typeof settings.get('nodeOpacity'), 'number');
for (const weight of [10000, Infinity, NaN, -100, undefined]) {
    const width = settings.get('linkWidth')({weight});
    assert.ok(width >= 0.8 && width <= 3);
}
assert.equal(settings.get('linkDirectionalParticles'), 0, 'undirected exports must not suggest direction');
el('btn-pause-physics').onclick();
assert.equal(settings.get('enableNodeDrag'), false);
for (const node of nodes) {
    assert.equal(node.fx, node.x); assert.equal(node.fy, node.y); assert.equal(node.fz, node.z);
    assert.equal(node.vx, 0);
}
assert.equal(setCounts.has('pauseAnimation'), false);
settings.get('onNodeClick')(nodes[0]);
assert.ok(setCounts.get('cameraPosition') >= 1);
el('btn-claim-view').onclick();
const claim = settings.get('graphData').nodes[0];
assert.equal(claim.fx, undefined, 'new view must not collapse uninitialized nodes at origin');
Object.assign(claim, {x:7, y:8, z:9});
settings.get('onEngineTick')();
assert.equal(claim.fx, 7);
assert.equal(claim.fy, 8);
assert.equal(claim.fz, 9);
el('btn-page-view').onclick();
assert.equal(settings.get('graphData'), data);
assert.equal(settings.get('graphData').nodes[0], nodes[0]);
el('btn-pause-physics').onclick();
assert.equal(settings.get('enableNodeDrag'), true);
assert.equal(nodes[0].fx, 19);
assert.equal(nodes[1].fx, undefined);
assert.equal(claim.fx, undefined);
assert.equal(setCounts.get('d3ReheatSimulation'), 1);
assert.equal(setCounts.has('resumeAnimation'), false);
assert.equal(setCounts.get('graphData'), initialSets + 2);
""",
  )


def test_graph_summary_plain_text_cleanup_expand_and_original_safety(tmp_path):
  run_check(
    tmp_path,
    r"""
const el = id => document.getElementById(id);
settings.get('onNodeClick')(nodes[0]);
assert.equal(el('info-title').children[0].textContent, "根 <img onerror='boom'>");
assert.equal(el('summary-original').textContent, nodes[0].summary);
assert.equal(el('summary-original-details').open, false);
assert.equal(el('sources-details').open, false);
assert.equal(el('metrics-details').open, false);
assert.equal(el('summary-more').hidden, false);
assert.ok(el('info-summary').textContent.length <= 281);
assert.doesNotMatch(el('info-summary').textContent, /System Directive|\*\*|# 标题/);
assert.match(el('info-summary').textContent, /临床证据 资料/);
el('summary-more').onclick();
assert.ok(el('info-summary').textContent.length > 280);
assert.equal(el('summary-more').attributes['aria-expanded'], 'true');
el('summary-more').onclick();
assert.ok(el('info-summary').textContent.length <= 281);
assert.match(el('info-sources').children[0].children[0].href, /%E4%B8%AD%E6%96%87%25/);
const text = '<img src=x onerror=alert(1)>\nSystem Directive is discussed as evidence.\nDose * 2 > 1.\n```text\n保留代码内容\n```';
context.text = text;
const cleaned = vm.runInContext('summaryExcerpt(text)', context);
assert.match(cleaned, /<img src=x onerror=alert\(1\)>/); // text, never HTML insertion
assert.match(cleaned, /System Directive is discussed as evidence/);
assert.match(cleaned, /Dose \* 2 > 1/);
assert.match(cleaned, /保留代码内容/);
assert.equal(vm.runInContext("summaryExcerpt('<!-- Template: placeholder -->正文')", context), '正文');
assert.equal(vm.runInContext("summaryExcerpt('*[System Directive: template]*')", context), '');
assert.equal(el('info-summary').innerHTML, '');
""",
  )
