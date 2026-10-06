"""Science uses Harbor mechanics while retaining separate dataset/version bindings."""
import json, shutil
import pytest
from ctxpress import benchmarks
from ctxpress.harness import eval_plan, eval_inputs, evaluation, task
from test_original_benchmarks import terminal_data


def data(tmp_path):
    root=terminal_data(tmp_path)
    (root/'dataset_manifest.json').write_text(json.dumps(dict(dataset='terminal-bench-science',revision='0.1.0')), encoding='utf-8')
    return root


def test_science_requires_provenance_and_cannot_relabel_terminal_tasks(tmp_path):
    root=terminal_data(tmp_path); adapter=benchmarks.get('terminal-bench-science')
    with pytest.raises(ValueError,match='dataset_manifest'):adapter.task_instances(root)
    (root/'dataset_manifest.json').write_text(json.dumps(dict(dataset='terminal-bench',revision='4.0.0')), encoding='utf-8')
    with pytest.raises(ValueError,match='Science'):adapter.task_instances(root)


def test_science_task_freezing_preserves_identity_after_original_data_is_removed(tmp_path):
    root=data(tmp_path); adapter=benchmarks.get('terminal-bench-science'); found=adapter.task_instances(root)[0]
    assert found['benchmark']=='terminal-bench-science'
    assert found['evaluation']['dataset']['revision']=='0.1.0'
    binary=tmp_path/'bin';binary.mkdir();(binary/'codex').write_bytes(b'fixture')
    cfg=dict(schema='ctxpress.eval',version=1,benchmark='terminal-bench-science',start_mode='task_start',scope='benchmark',
        model='fixture',backend='codex_docker',tasks=[found['id']],environment=dict(data=str(root),bindir=str(binary)),
        methods=[{'class':'NoCompaction'}])
    plan=eval_plan.compile_plan(cfg); directory=evaluation.prepare(plan,tmp_path/'run'); shutil.rmtree(root)
    restored=evaluation._verified_plan(directory);_,paths=eval_inputs.execution(restored,directory/'inputs')
    copied=task.remap(restored['jobs'][0]['task'],paths)
    assert adapter.agent_instruction(copied)=='Repair the terminal program.'
    assert not any('/solution/' in item['path'].replace('\\','/') for item in copied['inputs'])
    assert plan['benchmark']['execution_supported'] and plan['missing_environment_files']
    report=tmp_path/'official-result.json';report.write_text(json.dumps(dict(task_name=copied['id'],verifier_result={'rewards':{'reward':0.5}})), encoding='utf-8')
    assert adapter.read_grade(copied,report)['rewards']=={'reward':0.5}
    with pytest.raises(ValueError,match='another benchmark'):benchmarks.get('terminal-bench').agent_instruction(copied)
    with pytest.raises(ValueError,match='another benchmark'):benchmarks.get('terminal-bench').read_grade(copied,report)
