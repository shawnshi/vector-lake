"""One explicitly selected, hermetic backend lane. No deployment or production probes."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter

REPO = Path(__file__).resolve().parents[1]
TESTS = ('tests/test_runtime_identity.py', 'tests/test_business_acceptance.py',
         'tests/test_outbox_fencing.py', 'tests/test_projection_ordering.py',
         'tests/test_index_write_scope.py', 'tests/test_backup_verification.py',
         'tests/test_embedding_deadline.py', 'tests/test_projection_cost_scope.py')


CRITICAL_CASES = {
    ('tests.test_business_acceptance', 'test_create_update_delete_and_stale_publish_preserve_business_truth'),
    ('tests.test_business_acceptance', 'test_mcp_identity_protocol_is_live_readonly_not_production_acceptance'),
    ('tests.test_runtime_identity', 'test_live_report_json_and_native_identity_are_observations'),
    ('tests.test_runtime_identity', 'test_loaded_bytecode_mismatch_is_not_reported_as_current_disk'),
    ('tests.test_runtime_identity', 'test_probe_never_executes_current_source'),
    ('tests.test_runtime_identity', 'test_not_loaded_and_wrong_path_fail_honestly'),
    ('tests.test_runtime_identity', 'test_unreadable_or_invalid_current_source_is_not_healthy'),
    ('tests.test_runtime_identity', 'test_wrapped_function_code_and_lineno_only_changes'),
    ('tests.test_runtime_identity', 'test_missing_function_and_globals_are_not_overclaimed'),
    ('tests.test_runtime_identity', 'test_missing_native_file_is_not_a_successful_identity'),
    ('tests.test_runtime_identity', 'test_mcp_surface_is_readonly_structured_json'),
    ('tests.test_runtime_identity', 'test_code_shape_supports_python310_missing_optional_attributes'),
}


def _verify_critical_cases(path):
    with path.open('rb') as handle:
        data = handle.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise RuntimeError('Critical-case report exceeds budget')
    root = ET.fromstring(data)
    cases = root.findall('.//testcase')
    seen = Counter((case.get('classname'), case.get('name')) for case in cases)
    passed = Counter((case.get('classname'), case.get('name')) for case in cases if not any(case.find(tag) is not None for tag in ('skipped', 'failure', 'error')))
    if any(seen[key] != 1 or passed[key] != 1 for key in CRITICAL_CASES):
        raise RuntimeError('Critical business/identity assertions were not executed once and passed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=('native', 'fallback'), required=True)
    parser.add_argument('--native-dir', type=Path)
    parser.add_argument('--output', type=Path, required=True, help='New task-local directory; existing paths are rejected')
    args = parser.parse_args(argv)
    if args.backend == 'native' and args.native_dir is None:
        parser.error('Native requires a source-bound task build directory')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cmd = [sys.executable, '-B', str(REPO / 'scripts/ci_core.py'), 'test', '--backend', args.backend,
           '--receipt', str(output / 'backend-probe.json')]
    if args.native_dir is not None:
        cmd += ['--native-dir', str(args.native_dir.resolve())]
    cmd += ['--', '-q', '-o', 'junit_family=xunit1', '--junitxml', str(output / 'tests.xml'), *TESTS]
    env = dict(os.environ)
    for name in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS'):
        env.pop(name, None)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    with (output / 'tests.txt').open('wb') as log:
        child = subprocess.Popen(cmd, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
                                 start_new_session=os.name != 'nt')
        try:
            code = child.wait(timeout=130)
        except subprocess.TimeoutExpired:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(child.pid), '/T', '/F'], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            else:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=10)
            return 1  # no successful acceptance receipt for timeouts
    if code != 0:
        return code
    probe = json.loads((output / 'backend-probe.json').read_text())
    if probe['exit_code'] != 0 or any(probe[phase]['backend'] != args.backend for phase in ('start', 'end')):
        raise RuntimeError('Backend evidence does not match the requested lane')
    _verify_critical_cases(output / 'tests.xml')
    (output / 'acceptance.json').write_text(json.dumps({'status': 'synthetic_business_contracts_passed',
        'backend': args.backend, 'tests': TESTS, 'critical_cases_executed_once_and_passed': sorted(CRITICAL_CASES), 'backend_probe': probe,
        'production_acceptance': 'not_established', 'deployment': 'not_performed',
        'limits': 'Selected synthetic fault/CRUD oracles and in-process MCP only; not real corpus/provider/service rollout or backend graph equivalence'}, indent=2), encoding='utf-8')
    print(f'{args.backend}: synthetic contracts passed; artifacts: {output}; production acceptance NOT established')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
