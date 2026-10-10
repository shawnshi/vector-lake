"""Python3.10 compatible link/junction detection must not weaken lexical path rejection."""
import hashlib
import json
from pathlib import Path
import os
import stat
import subprocess
from types import SimpleNamespace
import pytest


def test_owner_paths_do_not_require_new_path_is_junction_api(isolated_memory, monkeypatch):
    from vector_lake import controlled_recompile as cr, ingest_model_contract as contract
    source = isolated_memory / 'raw' / 'scope.md'
    source.write_text('Synthetic public scope.')
    purpose = isolated_memory / 'purpose.md'
    purpose.write_text('---\nevidence_tiers:\n  primary: Primary\n---\nPrivate-looking body not needed by policy reader.\n')
    approval = isolated_memory / 'approval.json'
    approval.write_text('{}')
    with monkeypatch.context() as old_python:
        for cls in type(source).__mro__:
            if 'is_junction' in cls.__dict__:
                old_python.delattr(cls, 'is_junction')
        assert not hasattr(type(source), 'is_junction')
        assert cr.raw_path(str(source)) == source
        assert cr.fingerprint(str(source))['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert cr._json_file(str(approval), hashlib.sha256(approval.read_bytes()).hexdigest()) == {}
        assert contract.read_evidence_tiers_only()['primary'] == 'Primary'


def test_plain_missing_and_symlink_paths(tmp_path):
    from vector_lake.path_safety import is_link_or_junction
    file = tmp_path / 'plain.md'; file.write_text('fixture')
    assert not is_link_or_junction(file) and not is_link_or_junction(tmp_path / 'missing')
    link = tmp_path / 'link.md'
    try:
        link.symlink_to(file)
    except OSError:
        pytest.skip('Host does not allow unprivileged symlink creation')
    assert is_link_or_junction(link)


@pytest.mark.parametrize('tag,expected',[(0xA0000003,True),(0xA000000C,True),(None,True),(0x9000001A,False)])
def test_windows_reparse_tags_reject_mount_and_symlink_and_unknown(monkeypatch,tag,expected):
    from vector_lake import path_safety
    class LegacyPath:
        def lstat(self):
            return SimpleNamespace(st_mode=stat.S_IFDIR,st_file_attributes=0x400,st_reparse_tag=tag)
    with monkeypatch.context() as windows:
        windows.setattr(path_safety.os,'name','nt')
        assert path_safety.is_link_or_junction(LegacyPath()) is expected


def test_real_windows_junction_is_rejected_in_owner_source_path(isolated_memory):
    if os.name != 'nt':
        pytest.skip('Windows junction; Linux exercises real symlink path')
    from vector_lake import controlled_recompile as cr
    from vector_lake.path_safety import is_link_or_junction
    target = isolated_memory / 'raw' / 'target'; target.mkdir()
    source = target / 'scope.md'; source.write_text('fixture')
    link = isolated_memory / 'raw' / 'junction'
    result = subprocess.run(['cmd','/c','mklink','/J',str(link),str(target)],capture_output=True,timeout=10)
    assert result.returncode == 0, result.stderr
    try:
        assert is_link_or_junction(link)
        with pytest.raises(ValueError,match='link/junction'):
            cr.raw_path(str(link / 'scope.md'))
    finally:
        link.rmdir()  # remove the link itself, never the target tree
