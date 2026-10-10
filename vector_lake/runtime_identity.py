"""Read-only, point-in-time process observations; never a deployment/health approval."""
from __future__ import annotations

import hashlib
import importlib.machinery
import inspect
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import types

PACKAGE = Path(__file__).resolve().parent
MODULE_PROBES = {
    'vector_lake.db_store': ('transaction', 'claim_mutation_outbox'),
    'vector_lake.mutation_coordinator': ('execute_mutation_batch', 'materialize_markdown_projection'),
    'vector_lake.indexer': ('update_index_items', '_calculate_weighted_edges'),
    'vector_lake.watchdog_app': ('process_mutation_outbox_batch',),
    'vector_lake.mcp_server': ('runtime_identity',),
    'vector_lake.runtime_identity': ('runtime_identity',),
}


def _constant(value):
    if isinstance(value, types.CodeType):
        return ['code', _code_shape(value)]
    if isinstance(value, tuple):
        return ['tuple', [_constant(v) for v in value]]
    if isinstance(value, frozenset):
        return ['frozenset', sorted((_constant(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))]
    if isinstance(value, bytes):
        return ['bytes', value.hex()]
    if isinstance(value, float):
        return ['float', struct.pack('!d', value).hex()]
    if isinstance(value, complex):
        return ['complex', struct.pack('!dd', value.real, value.imag).hex()]
    if value is Ellipsis:
        return ['ellipsis']
    if value is None or isinstance(value, (str, int, bool)):
        return [type(value).__name__, value]
    raise TypeError('Unsupported code constant')


def _code_shape(code):
    # Deliberately ignore file paths, line numbers and tracing tables, not opcode/exception semantics.
    return {'bytecode': code.co_code.hex(), 'exceptiontable': getattr(code, 'co_exceptiontable', b'').hex(),
            'args': [code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount],
            'flags': code.co_flags, 'nlocals': code.co_nlocals, 'stacksize': code.co_stacksize,
            'names': code.co_names, 'varnames': code.co_varnames, 'freevars': code.co_freevars,
            'cellvars': code.co_cellvars, 'qualname': getattr(code, 'co_qualname', code.co_name),
            'constants': [_constant(v) for v in code.co_consts]}


def _code_hash(code):
    return hashlib.sha256(json.dumps(_code_shape(code), sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


def _module_record(name: str, expected_path: Path, functions: tuple[str, ...]) -> dict:
    module = sys.modules.get(name)
    if module is None:
        return {'status': 'not_loaded'}
    record = {'loaded_path': str(getattr(module, '__file__', '')), 'functions': {}}
    try:
        if Path(record['loaded_path']).resolve() != expected_path.resolve():
            return {**record, 'status': 'module_origin_mismatch'}
        with expected_path.open('rb') as handle:
            data = handle.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024:
            raise ValueError('Source exceeds probe budget')
        record['source_sha256'] = hashlib.sha256(data).hexdigest()
        compiled = compile(data, str(expected_path), 'exec', dont_inherit=True, optimize=sys.flags.optimize)
        # Only the top-level functions named in the explicit probe allowlist. No source execution.
        codes = {value.co_name: value for value in compiled.co_consts if isinstance(value, types.CodeType)}
        mismatch = False
        for function in functions:
            actual = inspect.unwrap(getattr(module, function, None))
            if not isinstance(actual, types.FunctionType) or function not in codes:
                record['functions'][function] = {'status': 'unavailable'}
                mismatch = True
                continue
            loaded = _code_hash(actual.__code__)
            current = _code_hash(codes[function])
            record['functions'][function] = {'loaded_code_sha256': loaded, 'current_source_code_sha256': current, 'matches': loaded == current}
            mismatch |= loaded != current
        return {**record, 'status': 'function_code_mismatch' if mismatch else 'matches_function_code'}
    except (OSError, SyntaxError, ValueError, TypeError) as exc:
        return {**record, 'status': 'source_unverifiable', 'error_type': type(exc).__name__}


def _native_record() -> dict:
    core = sys.modules.get('vector_lake_core')
    if core is None:
        return {'status': 'not_loaded'}
    binaries = {}
    for name, module in list(sys.modules.items()):
        if name == 'vector_lake_core' or name.startswith('vector_lake_core.'):
            filename = getattr(module, '__file__', '') or ''
            if any(filename.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES):
                binaries[Path(filename).resolve()] = module
    if len(binaries) != 1:
        return {'status': 'unverifiable', 'reason': 'Expected exactly one loaded core extension origin'}
    path, = binaries
    try:
        # This hashes disk bytes, NOT a claim to hash the already-mapped native image.
        digest = hashlib.sha256()
        total = 0
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                total += len(chunk)
                if total > 256 * 1024 * 1024:
                    raise ValueError('Native binary exceeds probe budget')
                digest.update(chunk)
        return {'status': 'loaded', 'binary_path': str(path), 'version': core.version(), 'on_disk_binary_sha256': digest.hexdigest()}
    except (OSError, AttributeError, TypeError, ValueError, RuntimeError) as exc:
        return {'status': 'unverifiable', 'error_type': type(exc).__name__}


def _checkout_record() -> dict:
    try:
        commit = subprocess.check_output(['git', 'rev-parse', '--verify', 'HEAD'], cwd=PACKAGE.parent, stderr=subprocess.DEVNULL, timeout=1).decode().strip()
        dirty = subprocess.check_output(['git', '--no-optional-locks', 'status', '--porcelain', '--untracked-files=no'], cwd=PACKAGE.parent, stderr=subprocess.DEVNULL, timeout=1)
        return {'commit': commit, 'tracked_worktree_dirty': bool(dirty), 'scope': 'disk checkout; not a loaded-code identifier; excludes untracked files'}
    except (OSError, subprocess.SubprocessError) as exc:
        return {'status': 'unavailable', 'error_type': type(exc).__name__}


def runtime_identity() -> dict:
    """Observe this caller process without importing unloaded core/application modules or opening a DB."""
    return {'schema_version': 1, 'pid': os.getpid(), 'python': sys.version.split()[0],
            'checkout': _checkout_record(),
            'modules': {name: _module_record(name, PACKAGE / (name.rsplit('.', 1)[1]+'.py'), functions) for name, functions in MODULE_PROBES.items()},
            'native': _native_record(), 'production_acceptance': 'not_established',
            'scope_limitations': 'Selected unwrapped function code only; excludes decorator wrappers, defaults, globals, import-time state and whole-module equivalence. Disk native SHA is not mapped-image SHA. No release binding, health approval or production acceptance. Not one atomic process/filesystem snapshot.'}
