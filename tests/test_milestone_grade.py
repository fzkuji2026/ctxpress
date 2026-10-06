"""Reject unbound native scoring evidence before invoking the author's collector."""
import json,sys
from pathlib import Path
from types import ModuleType
import pytest
from ctxpress.benchmarks.milestone_data import itinerary
from ctxpress.harness import milestone_grade
from test_task_instances import dataset


def fixture(tmp_path,monkeypatch):
    found=itinerary(dataset(tmp_path));code=tmp_path/'code';trial=tmp_path/'trial';trial.mkdir()
    module=ModuleType('harness.e2e.collect_results');module.__file__=str(code/'harness/e2e/collect_results.py')
    def forbidden(*args,**kwargs):pytest.fail('malformed evidence reached official scoring')
    for name in ('load_e2e_results','authoritative_cells','compute_repo_summary'):setattr(module,name,forbidden)
    for name in ('harness','harness.e2e'):
        package=ModuleType(name);package.__path__=[];monkeypatch.setitem(sys.modules,name,package)
    monkeypatch.setitem(sys.modules,module.__name__,module)
    summary=dict(repo_name=found['id'],agent_name='codex',total_milestones=2,results={
        'M2':dict(attempt=0,dag_status='completed',eval_status='passed',test_summary={'total':1})})
    return found,code,trial,summary


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value), encoding='utf-8')


def test_missing_native_summary_never_invents_a_failed_or_passed_trial(tmp_path,monkeypatch):
    found,code,trial,_=fixture(tmp_path,monkeypatch)
    actual=milestone_grade.read(found,code,trial)
    assert actual['resolved'] is None and actual['official_metrics'] is None and actual['failure_kind']=='grading_error'


def test_result_copy_must_match_the_hashed_evidence_even_when_source_reverts(tmp_path,monkeypatch):
    found,code,trial,summary=fixture(tmp_path,monkeypatch)
    source=trial/'evaluation/summary.json';write(source,summary)
    original=Path.read_bytes
    changed=dict(summary,repo_name='changed-during-read')
    monkeypatch.setattr(Path,'read_bytes',lambda path:json.dumps(changed).encode() if path==source else original(path))
    with pytest.raises(ValueError,match='copy differs'):milestone_grade.read(found,code,trial)


@pytest.mark.parametrize('change',['wrong-repo','wrong-agent','boolean-total','wrong-attempt','boolean-attempt',
    'negative-count','string-verdict','wrong-result-id','unselected-cell','symlink-result','invalid-json'])
def test_native_reader_refuses_malformed_or_foreign_results(tmp_path,monkeypatch,change):
    found,code,trial,summary=fixture(tmp_path,monkeypatch)
    result=dict(milestone_id='M2',resolved=True,test_summary={'total':1})
    if change=='wrong-repo':summary['repo_name']='other'
    if change=='wrong-agent':summary['agent_name']='claude-code'
    if change=='boolean-total':summary['total_milestones']=True
    if change=='wrong-attempt':summary['results']['M2']['attempt']=1
    if change=='boolean-attempt':summary['results']['M2']['attempt']=False
    if change=='negative-count':result['test_summary']['total']=-1
    if change=='string-verdict':result['resolved']='true'
    if change=='wrong-result-id':result['milestone_id']='M1'
    write(trial/'evaluation/summary.json',summary)
    file=trial/'evaluation/M2/evaluation_result.json';write(file,result)
    if change=='unselected-cell':write(trial/'evaluation/foreign/evaluation_result.json',result)
    if change=='invalid-json':file.write_text('null', encoding='utf-8')
    if change=='symlink-result':
        source=trial/'outside.json';write(source,result);file.unlink()
        try:file.symlink_to(source)
        except OSError:pytest.skip('platform cannot create symbolic links')
    with pytest.raises(ValueError):milestone_grade.read(found,code,trial)
