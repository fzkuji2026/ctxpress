"""Benchmark discovery and selection are read-only; fixtures are not real scores."""
import copy, json, subprocess
import pytest
from ctxpress import benchmarks
from ctxpress.__main__ import main
from ctxpress.harness.jobs import plan as eval_plan, queue as evaluation
from ctxpress.harness.results.report import report, write_report
from test_eval_inputs import inputs


def test_discovery_does_not_start_processes_or_import_local_helpers(tmp_path, monkeypatch, capsys):
    cfg = inputs(tmp_path)
    for name in ('miss_probe_docker.py', 'c03_run.py', 'multi_probe.py'):
        (tmp_path/'scripts'/name).write_text('raise RuntimeError("helper was executed")\n', encoding='utf-8')
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **kw: pytest.fail('discovery launched a process'))
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('discovery invoked a tool'))
    main(['eval', 'benchmarks'])
    adapters = json.loads(capsys.readouterr().out)['benchmarks']
    by_name = {row['name']:row for row in adapters}
    assert set(by_name) == {'swe-milestone','swe-bench','swe-bench-verified','swe-bench-lite','terminal-bench','terminal-bench-science','deep-swe','swe-polybench','swe-bench-pro','bigcodebench','browsecomp-plus','multi-swe-bench','swe-bench-multilingual'}
    assert by_name['swe-milestone']['from_task_start'] is False
    assert by_name['terminal-bench']['execution_supported'] and not by_name['terminal-bench']['real_run_verified']
    main(['eval', 'tasks', '--scripts', cfg['environment']['scripts'], '--min-context', '128000', '--gradable'])
    found = json.loads(capsys.readouterr().out)
    assert found['experiments_started'] == 0 and found['count'] == 1
    assert found['tasks'][0]['id'] == 'n3-j14' and found['tasks'][0]['context_tokens'] == 130000
    assert found['tasks'][0]['milestone'] == 'milestone_002'


def test_task_filters_do_not_imply_every_catalog_entry_can_be_graded(tmp_path, capsys):
    cfg = inputs(tmp_path)
    catalog = tmp_path/'scripts/valid_points.json'
    rows = json.loads(catalog.read_text(encoding='utf-8'))
    rows += [dict(n=9, j=1, src='recorded.jsonl', prefix_tokens=200000),
             dict(n=1, j=13, src='recorded.jsonl', prefix_tokens=30000)]
    catalog.write_text(json.dumps(rows), encoding='utf-8')
    main(['eval', 'tasks', '--scripts', cfg['environment']['scripts'], '--min-context', '128000'])
    found = json.loads(capsys.readouterr().out)
    assert [task['id'] for task in found['tasks']] == ['n3-j14', 'n9-j1']
    assert found['tasks'][1]['official_grading_mapped'] is False
    main(['eval', 'tasks', '--scripts', cfg['environment']['scripts'], '--min-context', '128000', '--gradable'])
    assert json.loads(capsys.readouterr().out)['count'] == 1


def test_task_ids_expand_to_frozen_boundaries_and_benchmark_identity(tmp_path):
    cfg = inputs(tmp_path)
    del cfg['boundaries']
    cfg.update(benchmark='swe-milestone', tasks=['n3-j14'])
    original = copy.deepcopy(cfg)
    plan = eval_plan.compile_plan(cfg)
    assert cfg == original
    assert plan['config']['boundaries'] == [dict(id='n3-j14', n=3, j=14, context_tokens=130000)]
    assert 'tasks' not in plan['config']
    assert plan['benchmark']['suite'] == 'local-recorded-boundaries'
    assert str(tmp_path/'scripts/recorded.jsonl') in plan['artifacts']
    changed = copy.deepcopy(plan)
    changed['benchmark']['suite'] = 'entire-benchmark'
    with pytest.raises(ValueError, match='changed'):
        eval_plan.verify(changed, check_inputs=False)


@pytest.mark.parametrize('ids', [[], ['n3-j14','n3-j14'], ['missing'], 'n3-j14', [True]])
def test_invalid_task_selections_do_not_silently_expand(tmp_path, ids):
    cfg = inputs(tmp_path)
    del cfg['boundaries']
    cfg['tasks'] = ids
    with pytest.raises(ValueError, match='task'):
        eval_plan.compile_plan(cfg)


def test_explicit_boundaries_and_task_ids_cannot_compete(tmp_path):
    cfg = inputs(tmp_path); cfg['tasks'] = ['n3-j14']
    with pytest.raises(ValueError, match='not both'):
        eval_plan.compile_plan(cfg)


def test_unsupported_benchmark_is_not_redirected_to_existing_backend(tmp_path):
    cfg = inputs(tmp_path); cfg['benchmark'] = 'not-registered'
    with pytest.raises(ValueError, match='unsupported benchmark'):
        eval_plan.compile_plan(cfg)
    cfg['benchmark'] = 'swe-milestone'; cfg['backend'] = 'other-agent'
    with pytest.raises(ValueError, match='supported benchmark backend'):
        eval_plan.compile_plan(cfg)


@pytest.mark.parametrize('change', ['duplicate', 'boolean-coordinate', 'invalid-tokens'])
def test_ambiguous_or_invalid_catalog_cannot_select_a_task(tmp_path, change):
    cfg = inputs(tmp_path); catalog = tmp_path/'scripts/valid_points.json'
    rows = json.loads(catalog.read_text(encoding='utf-8'))
    if change == 'duplicate': rows += copy.deepcopy(rows)
    if change == 'boolean-coordinate': rows[0]['n'] = True
    if change == 'invalid-tokens': rows[0]['prefix_tokens'] = True
    catalog.write_text(json.dumps(rows), encoding='utf-8')
    with pytest.raises(ValueError):
        benchmarks.get().select_tasks(cfg['environment']['scripts'], ['n3-j14'])


def test_adapter_dispatch_records_benchmark_identity(tmp_path, monkeypatch):
    from ctxpress.benchmarks.milestone import checkpoint_run as codex_docker
    received = []
    def run(n, j, entry, **kwargs):
        received.append((n, j, entry, kwargs))
        return dict(grade={})
    monkeypatch.setattr(codex_docker, 'run', run)
    result = benchmarks.get().run(dict(id='n3-j14', n=3, j=14), {'class':'NoCompaction'}, model='fixture')
    assert received == [(3, 14, {'class':'NoCompaction'}, dict(model='fixture'))]
    assert result['benchmark']['task_id'] == 'n3-j14' and result['benchmark']['from_task_start'] is False


def test_report_exposes_suite_and_does_not_invent_identity_for_old_plans(tmp_path):
    plan = eval_plan.compile_plan(inputs(tmp_path))
    directory = evaluation.prepare(plan, tmp_path/'run')
    result = report(directory)
    assert result['benchmark_declared'] and result['benchmark']['name'] == 'swe-milestone'
    write_report(directory)
    assert 'local-recorded-boundaries' in (directory/'report.html').read_text(encoding='utf-8')
    # Simulate the hashed legacy schema without rewriting frozen run artifacts.
    old = copy.deepcopy(plan); old.pop('benchmark'); old['config'].pop('benchmark')
    import hashlib
    old['sha256'] = hashlib.sha256(eval_plan.canonical({k:v for k,v in old.items() if k!='sha256'}).encode()).hexdigest()
    legacy = evaluation.prepare(old, tmp_path/'legacy')
    result = report(legacy)
    assert not result['benchmark_declared'] and result['benchmark'] is None
