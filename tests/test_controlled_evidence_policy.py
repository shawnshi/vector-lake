import io
import json
from pathlib import Path
import pytest
from vector_lake import ingest_model_contract, wiki_utils
from tests.test_controlled_recompile import scope_files, _dispatch, _claim
from scripts import ingest_model_pi_subagents as pi_adapter


def test_policy_reads_frontmatter_only(isolated_memory, monkeypatch):
    path = isolated_memory / 'purpose.md'
    header = b'---\nevidence_tiers:\n  secondary: A synthetic secondary source\nprivate_account: PRIVATE_HEADER_SENTINEL\n---\n'
    body = b'PRIVATE_PROSE_SENTINEL\xff'
    path.write_bytes(header + body)
    streams = []
    class Observed(io.BytesIO):
        def close(self):
            self.final_position = self.tell()
            super().close()
        def read(self, *args):
            raise AssertionError('Broad read is unauthorized')
    original = Path.open
    def bounded_open(p, mode='r', *args, **kwargs):
        if p == path:
            assert mode == 'rb' and kwargs.get('buffering') == 0
            handle = Observed(header + body); streams.append(handle); return handle
        return original(p, mode, *args, **kwargs)
    monkeypatch.setattr(wiki_utils, 'get_purpose_path', lambda: path)
    monkeypatch.setattr(Path, 'open', bounded_open)
    tiers = ingest_model_contract.read_evidence_tiers_only()
    assert tiers == {'secondary': 'A synthetic secondary source'}
    assert streams[0].final_position == len(header)
    assert 'PRIVATE' not in json.dumps(tiers)


@pytest.mark.parametrize('data', [b'---\nevidence_tiers: {}\n---\n', b'---\nevidence_tiers: {x: false}\n---\n', b'---\n' + b'x' * (16*1024), b'no YAML header'])
def test_policy_fails_closed(isolated_memory, monkeypatch, data):
    path = isolated_memory / 'purpose.md'; path.write_bytes(data)
    monkeypatch.setattr(wiki_utils, 'get_purpose_path', lambda: path)
    with pytest.raises(ValueError):
        ingest_model_contract.read_evidence_tiers_only()


def test_both_real_adapters_supply_only_tiers(scope_files):
    path = scope_files['memory'] / 'purpose.md'
    with path.open('ab') as handle:
        handle.write(b'\nPRIVATE_PROSE_SENTINEL\n')
    _dispatch(scope_files);task,p = _claim()
    expected = ingest_model_contract.read_evidence_tiers_only()
    for hydrate in (ingest_model_contract.build_cli_prompt, pi_adapter._brief):
        prompt = hydrate(task['task_packet'])
        assert '\"evidence_tier_policy\"' in prompt
        assert json.dumps(expected, ensure_ascii=False) in prompt
        assert 'PRIVATE_PROSE_SENTINEL' not in prompt
        assert p['lease_token'] not in prompt
