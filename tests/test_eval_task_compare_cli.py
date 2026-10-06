"""Comparison CLI reads existing evidence and only publishes a new analysis."""
import json
import sqlite3

import pytest

from ctxpress.harness.jobs import plan as eval_plan, queue as evaluation
from ctxpress.harness.results import task_compare as eval_task_compare
from test_task_start_plan import config


@pytest.fixture
def run(tmp_path):
    plan = eval_plan.compile_plan(config(tmp_path))
    directory = tmp_path / 'run'
    directory.mkdir()
    with sqlite3.connect(directory / 'jobs.sqlite') as connection:
        connection.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)')
        connection.execute('CREATE TABLE jobs (id TEXT PRIMARY KEY, spec TEXT, status TEXT)')
        connection.execute('INSERT INTO metadata VALUES (?, ?)', ('plan', json.dumps(plan)))
        for spec in plan['jobs']:
            connection.execute('INSERT INTO jobs VALUES (?, ?, ?)',
                               (spec['id'], json.dumps(spec), 'pending'))
    return directory, plan


def test_missing_run_does_not_create_a_database(tmp_path):
    directory = tmp_path / 'absent'
    with pytest.raises(ValueError, match='existing regular evaluation database'):
        evaluation.compare_results(directory, 'reference')
    assert not directory.exists()


def test_cli_passes_frozen_snapshot_and_keeps_database_unchanged(run, tmp_path, monkeypatch, capsys):
    directory, plan = run
    before = (directory / 'jobs.sqlite').read_bytes()
    analysis = {'schema': 'fixture-analysis', 'quality': None, 'note': '待评分'}
    def compare(actual_plan, jobs, *, reference, directory):
        assert actual_plan == plan
        assert reference == 'explicit-reference'
        assert len(jobs) == 1 and jobs[0]['status'] == 'pending'
        assert json.loads(jobs[0]['spec']) == plan['jobs'][0]
        assert directory == run[0]
        return analysis
    monkeypatch.setattr(eval_task_compare, 'compare', compare)
    output = tmp_path / 'analysis' / 'result.json'
    evaluation.main(['compare', '--directory', str(directory), '--reference', 'explicit-reference',
                     '--output', str(output)])
    assert json.loads(capsys.readouterr().out) == analysis
    assert json.loads(output.read_text(encoding='utf-8')) == analysis
    assert (directory / 'jobs.sqlite').read_bytes() == before


@pytest.mark.parametrize('target', ['existing', 'inputs', 'runtime', 'jobs', 'source', 'auth'])
def test_output_cannot_replace_or_pollute_frozen_evidence(run, tmp_path, monkeypatch, target):
    directory, plan = run
    existing = tmp_path / 'existing.json'
    existing.write_text('keep me', encoding='utf-8')
    source = next(iter(plan['input_trees'].values()))['tree']['root']
    from pathlib import Path
    destinations = {'existing': existing, 'source': Path(source) / 'new.json',
                    'auth': tmp_path / 'auth.json'}
    destination = destinations.get(target, directory / target / 'analysis.json')
    monkeypatch.setattr(eval_task_compare, 'compare', lambda *a, **kw: pytest.fail('unsafe output reached analysis'))
    with pytest.raises(ValueError, match='new file outside frozen evidence'):
        evaluation.compare_results(directory, 'reference', destination)
    assert existing.read_text(encoding='utf-8') == 'keep me'
    if target != 'existing':
        assert not destination.exists()


def test_output_created_during_analysis_is_not_overwritten(run, tmp_path, monkeypatch):
    directory, _ = run
    destination = tmp_path / 'raced.json'
    def compare(*args, **kwargs):
        destination.write_text('other writer', encoding='utf-8')
        return {'result': 'ours'}
    monkeypatch.setattr(eval_task_compare, 'compare', compare)
    with pytest.raises(FileExistsError):
        evaluation.compare_results(directory, 'reference', destination)
    assert destination.read_text(encoding='utf-8') == 'other writer'


def test_rejected_analysis_does_not_publish_an_empty_file(run, tmp_path, monkeypatch):
    directory, _ = run
    destination = tmp_path / 'invalid.json'
    monkeypatch.setattr(eval_task_compare, 'compare', lambda *a, **kw: {'invalid': float('nan')})
    with pytest.raises(ValueError):
        evaluation.compare_results(directory, 'reference', destination)
    assert not destination.exists()


def test_cli_requires_explicit_reference(run, capsys):
    with pytest.raises(SystemExit) as error:
        evaluation.main(['compare', '--directory', str(run[0])])
    assert error.value.code == 2
    assert '--reference' in capsys.readouterr().err
