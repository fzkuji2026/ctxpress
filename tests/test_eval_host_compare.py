"""Client metadata fallback is a comparison confound, not task/model prose."""
import json
from pathlib import Path
import pytest
from test_eval_task_compare import fixture, run

MESSAGE = ('Model metadata for `main` not found. Defaulting to fallback metadata; '
           'this can degrade performance and cause issues.')


def cli_log(data, index, row):
    _, jobs, directory = data
    folder = directory / 'jobs' / jobs[index]['id'] / 'attempt-1'
    path = folder / 'codex.txt'
    path.write_text(json.dumps(row) + '\n', encoding='utf-8')
    return path


def host_session(data, index, text):
    _, jobs, directory = data
    folder = directory / 'jobs' / jobs[index]['id'] / 'attempt-1'
    path = folder / 'sessions/rollout-host.jsonl';path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(dict(type='session_meta', payload=dict(base_instructions={'text':text}))) + '\n', encoding='utf-8')
    return path


def test_structured_client_fallback_excludes_comparison_but_preserves_cost_and_original_grade(tmp_path):
    data = fixture(tmp_path)
    original = json.loads(data[1][1]['result'])
    path = cli_log(data, 1, dict(type='item.completed', item=dict(type='error', message=MESSAGE)))
    out = run(data);row = out['jobs'][1]
    assert not row['quality_admissible'] and row['api_cost_complete'] and row['api_cost_usd'] > 0
    assert not out['candidates'][0]['quality_complete'] and out['candidates'][0]['api_cost_complete']
    candidate = out['candidates'][0]
    assert not candidate['api_cost_comparable'] and candidate['costs']['api_cost_delta_usd'] is None
    assert candidate['costs']['candidate_api_cost_usd'] == row['api_cost_usd']
    assert candidate['coverage']['cost_pairs'] == 1 and candidate['coverage']['cost_comparable_pairs'] == 0
    assert 'incompatible Codex host' in row['quality_reasons'][0]
    assert row['host_observation']['model_metadata_fallbacks'][0] == dict(kind='model_metadata_fallback',
        model='main', source=str(path), line=1)
    assert json.loads(data[1][1]['result']) == original
    assert json.loads((path.parent/'result.json').read_text(encoding='utf-8')) == original


@pytest.mark.parametrize('row', [
    dict(type='item.completed',item=dict(type='agent_message',text=MESSAGE)),
    dict(type='item.completed',item=dict(type='command_execution',aggregated_output=MESSAGE)),
    dict(type='item.completed',item=dict(type='error',message='quoted: '+MESSAGE)),
    dict(type='item.completed',item=dict(type='error',message=MESSAGE.replace('`main`','`other`'))),
    dict(type='response_item',payload=dict(type='message',role='assistant',content=MESSAGE)),
])
def test_agent_text_tool_output_or_unrelated_error_is_not_metadata_fallback(tmp_path, row):
    data=fixture(tmp_path);cli_log(data,1,row)
    assert run(data)['candidates'][0]['quality_complete']


def test_different_retained_base_instructions_block_pair_without_changing_valid_scores_or_costs(tmp_path):
    data=fixture(tmp_path);host_session(data,0,'host A');host_session(data,1,'host B')
    out=run(data);pair=out['candidates'][0]
    assert all(row['quality_admissible'] and row['api_cost_complete'] for row in out['jobs'])
    assert not pair['quality_complete'] and pair['api_cost_complete']
    assert not pair['api_cost_comparable'] and pair['costs']['api_cost_delta_usd'] is None
    assert pair['pairs'][0]['api_cost_delta_usd'] is None
    assert pair['metrics']['resolved']['delta_mean'] is None
    assert 'paired Codex base instructions differ' in pair['pairs'][0]['reasons'][-1]


def test_matching_host_instructions_and_missing_historical_logs_have_explicit_observation(tmp_path):
    data=fixture(tmp_path);host_session(data,0,'same');host_session(data,1,'same')
    out=run(data);assert out['candidates'][0]['quality_complete']
    assert out['candidates'][0]['api_cost_comparable']
    assert out['jobs'][0]['host_observation']['base_instruction_sha256'] == out['jobs'][1]['host_observation']['base_instruction_sha256']
    for path in data[2].rglob('rollout-host.jsonl'):path.unlink()
    out=run(data);assert out['candidates'][0]['quality_complete']
    assert out['jobs'][0]['host_observation']['base_instruction_sha256'] == []


def test_host_log_alias_is_rejected_before_reading_credential(tmp_path, monkeypatch):
    data=fixture(tmp_path);path=cli_log(data,1,{})
    secret=tmp_path/'auth.json';secret.write_text('must not be read', encoding='utf-8');path.unlink();path.symlink_to(secret)
    original=Path.read_bytes
    def guarded(p):
        assert p.resolve()!=secret.resolve()
        return original(p)
    monkeypatch.setattr(Path,'read_bytes',guarded)
    row=run(data)['jobs'][1]
    assert not row['quality_admissible'] and row['api_cost_complete']
    assert row['quality_reasons']


def test_native_author_stdout_uses_same_structured_client_warning_gate(tmp_path):
    data=fixture(tmp_path,'swe-milestone');_,jobs,directory=data
    folder=directory/'jobs'/jobs[1]['id']/'attempt-1'
    stdout=folder/'trial/log/agent_stdout.txt';stdout.parent.mkdir()
    stdout.write_text(json.dumps(dict(type='item.completed',item=dict(type='error',message=MESSAGE)))+'\n', encoding='utf-8')
    out=run(data);row=out['jobs'][1]
    assert not row['quality_admissible'] and row['api_cost_complete']
    assert row['host_observation']['model_metadata_fallbacks'][0]['source']==str(stdout)
