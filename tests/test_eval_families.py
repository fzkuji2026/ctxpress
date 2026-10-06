"""Eight fixed families share selection, sealed prices and lifecycle reports.

The scheduler subprocess is synthetic. Official execution/grading is exercised
separately by each adapter's tests; this fixture is not benchmark evidence.
"""
import json
from pathlib import Path

import pytest

from ctxpress import benchmarks
from ctxpress.__main__ import main
from ctxpress.harness import evaluation, eval_plan
from test_evaluation import fake_launch
from test_model_accounting import rates


NAMES = ['swe-milestone', 'swe-bench-verified', 'terminal-bench', 'terminal-bench-science',
         'deep-swe', 'swe-bench-pro', 'swe-polybench', 'bigcodebench']


def fixture_data(tmp_path, name):
    if name == 'swe-milestone':
        from test_task_instances import dataset
        return dataset(tmp_path)
    if name == 'swe-bench-verified':
        from test_original_benchmarks import swe_data
        return swe_data(tmp_path)[0]
    if name in ('terminal-bench', 'terminal-bench-science'):
        from test_original_benchmarks import terminal_data
        root = terminal_data(tmp_path)
        if name.endswith('science'):
            (root/'dataset_manifest.json').write_text(json.dumps({'dataset': name, 'revision': 'fixture-v1'}), encoding='utf-8')
        return root
    if name == 'deep-swe':
        from test_deep_swe import data
        return data(tmp_path)
    if name == 'swe-bench-pro':
        from test_swe_pro import data
        return data(tmp_path)
    if name == 'swe-polybench':
        from test_poly_bench import data
        return data(tmp_path)[0]
    from test_bigcode_bench import data
    return data(tmp_path)[0]


@pytest.mark.parametrize('name', NAMES)
def test_fixed_family_selection_plan_cancel_recover_resume_and_report(tmp_path, name, capsys, monkeypatch):
    root = fixture_data(tmp_path, name)
    main(['eval', 'tasks', '--benchmark', name, '--start-mode', 'task_start', '--data', str(root)])
    catalog = json.loads(capsys.readouterr().out)
    assert catalog['experiments_started'] == 0
    selected = catalog['tasks'][0]['id']
    binary = tmp_path/'bin'; binary.mkdir(); (binary/'codex').write_bytes(b'synthetic fixture')
    cfg = dict(schema='ctxpress.eval', version=1, benchmark=name, start_mode='task_start', scope='benchmark',
               model='main', tasks=[selected], backend='codex_docker', methods=[{'class': 'NoCompaction'}],
               repeats=2, workers=1, prices={'models': {'main': rates(), 'reflect': rates(1, .1, 2)}},
               environment={'data': str(root), 'bindir': str(binary)})
    source = tmp_path/'config.json'; source.write_text(json.dumps(cfg), encoding='utf-8')
    plan_path = tmp_path/'plan.json'
    main(['eval', 'plan', str(source), '--output', str(plan_path)])
    planned = json.loads(capsys.readouterr().out)
    assert planned['run_count'] == 2
    plan = eval_plan.load(plan_path, check_inputs=False)
    assert plan['config']['prices'] == cfg['prices']
    assert all(job['task']['id'] == selected for job in plan['jobs'])
    directory = evaluation.prepare(plan, tmp_path/'run')
    main(['eval', 'status', '--directory', str(directory)])
    assert json.loads(capsys.readouterr().out)['counts'] == {'pending': 2}
    main(['eval', 'cancel', '--directory', str(directory)])
    assert json.loads(capsys.readouterr().out)['counts'] == {'cancelled': 2}
    main(['eval', 'recover', '--directory', str(directory)])
    assert json.loads(capsys.readouterr().out)['control']['dispatch_stopped']
    def synthetic_start(path, folder, background=False, retry=False):
        assert background and retry
        return evaluation.schedule(folder, retry=True, launch=fake_launch, interval=.02)
    monkeypatch.setattr(evaluation, 'start', synthetic_start)
    main(['eval', 'resume', '--directory', str(directory), '--background'])
    json.loads(capsys.readouterr().out)
    main(['eval', 'status', '--directory', str(directory)])
    assert json.loads(capsys.readouterr().out)['counts'] == {'completed': 2}
    main(['eval', 'report', '--directory', str(directory)])
    written = json.loads(capsys.readouterr().out)
    report = json.loads(Path(written['report']).read_text(encoding='utf-8'))
    assert report['benchmark']['name'] == name
    assert report['evidence'].startswith('synthetic')
    assert report['methods'][0]['completed'] == 2
    assert report['methods'][0]['api_cost_at_declared_rates_usd'] is None  # fixture has legacy unidentified summaries
    assert Path(written['html']).is_file()


def test_family_scope_stays_fixed():
    assert len(benchmarks.FAMILIES) == 8
    assert {benchmarks.get(name).task_start_description()['name'] for name in NAMES} == set(NAMES)


@pytest.mark.parametrize('name,template', list(zip(NAMES, [
    'swe_milestone', 'swe_bench', 'terminal_bench', 'terminal_science',
    'deep_swe', 'swe_pro_v2', 'polybench', 'bigcodebench'])))
def test_shipping_template_compiles_with_real_local_task_schema(tmp_path, name, template):
    root = fixture_data(tmp_path, name)
    cfg = json.loads((Path(__file__).resolve().parents[1]/'configs'/(template+'.example.json')).read_text(encoding='utf-8'))
    binary = tmp_path/'bin'; binary.mkdir(); (binary/'codex').write_bytes(b'synthetic fixture')
    cfg['environment'] = {'data': str(root), 'bindir': str(binary)}
    cfg['tasks'] = [benchmarks.get(name).task_instances(root)[0]['id']]
    plan = eval_plan.compile_plan(cfg)
    assert plan['benchmark']['name'] == name and plan['run_count'] >= 1
