"""Official prediction binding must preserve patch newlines exactly."""
import json

import pytest

from ctxpress.harness.jobs import plan as eval_plan
from test_eval_task_compare import change_result, fixture, run, write


@pytest.mark.parametrize('patch', ['context\rremoved\n', 'context\r\nadded\r\n'])
@pytest.mark.parametrize('matching', [True, False])
def test_swe_prediction_binding_preserves_carriage_returns(tmp_path, patch, matching):
    data = fixture(tmp_path)
    plan, jobs, directory = data
    job = jobs[-1]
    folder = directory / 'jobs' / job['id'] / 'attempt-1'
    value = json.loads(job['result'])
    spec = json.loads(job['spec'])
    (folder / 'model.patch').write_bytes(patch.encode('utf-8'))
    predicted = patch if matching else patch.replace('\r\n', '\n').replace('\r', '\n')
    (folder / 'predictions.jsonl').write_text(json.dumps(dict(
        instance_id=spec['task']['id'], model_name_or_path=value['model'],
        model_patch=predicted)) + '\n', encoding='utf-8')
    artifacts = value['official_artifacts']
    for name in ('model.patch', 'predictions.jsonl'):
        artifacts[name]['sha256'] = eval_plan.file_sha256(folder / name)
    state_path = folder / 'swe-worker-result.json'
    state = json.loads(state_path.read_text(encoding='utf-8'))
    state['official_artifacts'] = artifacts
    write(state_path, state)
    change_result(job, directory, lambda result: result.update(official_artifacts=artifacts))
    before = {p: p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    candidate = run(data)['candidates'][0]
    assert candidate['quality_complete'] is matching
    assert candidate['api_cost_complete']
    assert before == {p: p.read_bytes() for p in folder.rglob('*') if p.is_file()}
    if not matching:
        assert any('official prediction does not bind' in reason
                   for reason in candidate['pairs'][0]['candidate']['quality_reasons'])
