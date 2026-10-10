"""Synthetic business oracles, real DB/outbox/index/FTS and in-process MCP transport."""
import asyncio
import json

import pytest

from vector_lake import db_store, mutation_coordinator
from vector_lake.watchdog_app import process_mutation_outbox_batch
from tests.test_mutation_coordinator import _write_purpose_contract, _named_source_content


def test_create_update_delete_and_stale_publish_preserve_business_truth(isolated_memory):
    _write_purpose_contract(isolated_memory)
    filename, key = 'Source_Acceptance.md', 'Source_Acceptance'
    a = _named_source_content('source_acceptance', 'acceptancealpha', 'Synthetic evidence A.')
    b = _named_source_content('source_acceptance', 'acceptancebeta', 'Synthetic evidence B.')
    target = isolated_memory / 'wiki' / filename
    assert mutation_coordinator.execute_mutation_plan(filename, content=a)[0]
    assert process_mutation_outbox_batch(backoff_base=0)['completed'] == 1
    old_id = db_store.get_connection().execute('SELECT id FROM mutation_outbox WHERE filename=?', (filename,)).fetchone()[0]
    assert any(row['node_key'] == key for row in db_store.search_wiki('acceptancealpha'))
    assert mutation_coordinator.execute_mutation_plan(filename, content=b)[0]
    with pytest.raises(db_store.MutationSuperseded):
        mutation_coordinator.materialize_markdown_projection(filename, 'update', a, outbox_id=old_id)
    assert target.read_text(encoding='utf-8') == b
    assert process_mutation_outbox_batch(backoff_base=0)['completed'] == 1
    canonical = db_store.get_connection().execute('SELECT data_json FROM entities WHERE f_page_key=?', (key,)).fetchone()
    entity = json.loads(canonical[0])
    assert entity['title'] == 'acceptancebeta' and entity['raw_text'] == 'Synthetic evidence B.\n'
    assert key in json.loads((isolated_memory / 'wiki/index.json').read_text())['nodes']
    assert any(row['node_key'] == key for row in db_store.search_wiki('acceptancebeta'))
    assert not db_store.search_wiki('acceptancealpha')
    assert mutation_coordinator.execute_mutation_plan(filename, is_delete=True)[0]
    stats = process_mutation_outbox_batch(backoff_base=0)
    assert stats['completed'] == 1 and stats['failed'] == stats['retrying'] == 0
    assert not target.exists()
    assert key not in json.loads((isolated_memory / 'wiki/index.json').read_text())['nodes']
    assert not db_store.search_wiki('acceptancebeta')
    current = db_store.get_connection().execute('SELECT status,mutation_type FROM mutation_outbox WHERE filename=? ORDER BY projection_generation DESC,id DESC LIMIT 1', (filename,)).fetchone()
    assert current['status'] == 'completed' and current['mutation_type'] == 'delete'


def test_mcp_identity_protocol_is_live_readonly_not_production_acceptance(isolated_memory):
    from fastmcp import Client
    from vector_lake import mcp_server

    async def check():
        async with Client(mcp_server.mcp) as client:
            tools = await client.list_tools()
            assert {'runtime_identity', 'doctor_vector_lake'} <= {tool.name for tool in tools}
            result = await client.call_tool('runtime_identity')
            assert not result.is_error
            report = json.loads(result.content[0].text)
            assert report['production_acceptance'] == 'not_established'
            assert report['modules']['vector_lake.db_store']['status'] == 'matches_function_code'
            assert report['modules']['vector_lake.watchdog_app']['status'] == 'matches_function_code'
            assert not list((isolated_memory / 'wiki').rglob('*.db'))
    asyncio.run(check())


def test_acceptance_requires_fresh_directory_and_explicit_native_build(tmp_path):
    from scripts import business_acceptance as gate
    old = tmp_path / 'old'
    old.mkdir()
    with pytest.raises(FileExistsError):
        gate.main(['--backend', 'fallback', '--output', str(old)])
    missing = tmp_path / 'missing'
    with pytest.raises(SystemExit) as error:
        gate.main(['--backend', 'native', '--output', str(missing)])
    assert error.value.code == 2 and not missing.exists()


def test_pytest_filter_and_plugin_environment_cannot_select_a_fake_gate(tmp_path, monkeypatch):
    from scripts import business_acceptance as gate
    monkeypatch.setenv('PYTEST_ADDOPTS', '--collect-only -k runtime_identity')
    monkeypatch.setenv('PYTEST_PLUGINS', 'fixture_untrusted_plugin')
    captured = {}
    class StopProbe(Exception):
        pass
    def intercept(*args, **kwargs):
        captured.update(kwargs['env'])
        raise StopProbe()
    monkeypatch.setattr(gate.subprocess, 'Popen', intercept)
    with pytest.raises(StopProbe):
        gate.main(['--backend', 'fallback', '--output', str(tmp_path / 'probe')])
    assert 'PYTEST_ADDOPTS' not in captured and 'PYTEST_PLUGINS' not in captured


def test_collect_only_filtered_or_skipped_junit_cannot_pass_gate(tmp_path):
    from scripts import business_acceptance as gate
    xml = tmp_path / 'tests.xml'
    xml.write_text('<testsuites><testsuite/></testsuites>')
    with pytest.raises(RuntimeError, match='Critical'):
        gate._verify_critical_cases(xml)
    cases = ''.join(f'<testcase classname="{module}" name="{name}"/>' for module, name in gate.CRITICAL_CASES)
    xml.write_text('<testsuites><testsuite>'+cases+'</testsuite></testsuites>')
    gate._verify_critical_cases(xml)
    xml.write_text('<testsuites><testsuite>'+cases.replace('/>', '><skipped/></testcase>', 1)+'</testsuite></testsuites>')
    with pytest.raises(RuntimeError, match='Critical'):
        gate._verify_critical_cases(xml)
    xml.write_text('<testsuites><testsuite>'+cases+cases+'</testsuite></testsuites>')
    with pytest.raises(RuntimeError, match='Critical'):
        gate._verify_critical_cases(xml)


def test_failed_child_cannot_emit_acceptance(tmp_path, monkeypatch):
    from scripts import business_acceptance as gate
    class Child:
        pid = 43210
        def wait(self, timeout):
            return 1
    monkeypatch.setattr(gate.subprocess, 'Popen', lambda *args, **kwargs: Child())
    output = tmp_path / 'failure'
    assert gate.main(['--backend', 'fallback', '--output', str(output)]) == 1
    assert not (output / 'acceptance.json').exists()


def test_timeout_targets_owned_process_tree_and_emits_no_success(tmp_path, monkeypatch):
    from scripts import business_acceptance as gate
    import os
    killed = []
    class Child:
        pid = 43210
        def wait(self, timeout):
            if timeout == 130:
                raise gate.subprocess.TimeoutExpired('fixture', timeout)
            return -9
    monkeypatch.setattr(gate.subprocess, 'Popen', lambda *args, **kwargs: Child())
    if os.name == 'nt':
        monkeypatch.setattr(gate.subprocess, 'run', lambda cmd, **kwargs: killed.append(cmd))
    else:
        monkeypatch.setattr(gate.os, 'killpg', lambda pid, signal: killed.append(pid))
    output = tmp_path / 'timeout'
    assert gate.main(['--backend', 'fallback', '--output', str(output)]) == 1
    assert (killed[0][2] == '43210') if os.name == 'nt' else (killed == [43210])
    assert not (output / 'acceptance.json').exists()
