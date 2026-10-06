"""Synthetic offline artifact contracts, never benchmark execution or scores."""
import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from ctxpress import benchmarks
from ctxpress.harness.jobs import inputs as eval_inputs, plan as eval_plan, trees as eval_trees, resources as task_resources
from ctxpress.harness.results import task_compare as eval_task_compare
from test_eval_families import fixture_data
from test_eval_task_compare import change_result, rates, run, write


LOCKED_CODEX_SOURCE = '''from harbor.agents.installed.codex import Codex

class LockedCodex(Codex):
    @staticmethod
    def name() -> str:
        return "codex-locked"
'''


def fixture(tmp_path, name='terminal-bench', *, repeats=1, tasks=1, variant='modern', prices=True):
    root = fixture_data(tmp_path, name)
    if name in ('terminal-bench', 'terminal-bench-science'):
        text = '[task]\nname="publisher/task-one"\n[environment]\ncpus=2\nmemory_mb=4096\n[verifier]\ntimeout_sec=120\n'
        if variant != 'shared':
            text += 'environment_mode="separate"\n[verifier.environment]\ncpus=2\nmemory_mb=4096\n'
            (root / 'task-one/tests/Dockerfile').write_text('FROM synthetic:verifier\n', encoding='utf-8')
        (root / 'task-one/task.toml').write_text(text, encoding='utf-8')
        write(root / 'dataset_manifest.json', dict(dataset=name, revision='fixture-v1'))
    if tasks == 2:
        shutil.copytree(root / 'task-one', root / 'task-two')
        p = root / 'task-two/task.toml'; p.write_text(p.read_text(encoding='utf-8').replace('task-one', 'task-two'), encoding='utf-8')
    selected = benchmarks.get(name).task_instances(root)
    trees = {}; deep = name == 'deep-swe'; pro = name == 'swe-bench-pro'
    frame = 'pier' if deep else 'harbor'; version = '0.3.1' if deep else '0.23.0'
    sources = ['pyproject.toml', f'src/{frame}/__init__.py', f'src/{frame}/trial/trial.py',
               f'src/{frame}/agents/installed/codex.py', f'src/{frame}/environments/docker/docker.py']
    if deep:
        sources += ['src/pier/trial/artifact_handler.py', 'src/pier/models/task/verifier_mode.py']
    elif variant != 'legacy':
        sources += ['src/harbor/trial/single_step.py', 'src/harbor/models/task/verifier_mode.py', 'src/harbor/environments/capabilities.py']
    files = {frame:sources, 'dependencies': [('datacurve_pier' if deep else 'harbor') + '-' + version + '.dist-info/METADATA']}
    if pro:
        files['pro_tooling'] = ['locked_codex.py', 'patch_replay.py']
    for key, names in files.items():
        base = tmp_path / 'official' / key
        for rel in names:
            p = base / rel; p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('[project]\nversion="' + version + '"\n' if rel == 'pyproject.toml' else
                         'Name: ' + ('datacurve-pier' if deep else 'harbor') + '\nVersion: ' + version + '\n' if rel.endswith('METADATA') else
                         LOCKED_CODEX_SOURCE if rel == 'locked_codex.py' else '# synthetic source\n', encoding='utf-8')
        trees[key] = eval_trees.capture(base, names, folder='unused')['tree']
    runtime = dict(python=str(Path(sys.executable).resolve()), sha256=eval_plan.file_sha256(sys.executable),
                   version=sys.version, platform='linux')
    images = dict(agent={'id':'sha256:' + '1'*64}, grading={'verifier':{'id':'sha256:' + ('1' if variant == 'shared' else '2')*64}})
    release = selected[0]['evaluation']['dataset']['revision']
    lock = task_resources.seal(dict(schema=task_resources.SCHEMA, version=1, benchmark=name, release=release,
        runtime=runtime, trees=trees, tasks={t['id']:dict(task_sha256=task_resources.task_digest(t), images=copy.deepcopy(images)) for t in selected}))
    resources = write(tmp_path / 'resources.json', lock)
    binary = tmp_path / 'bin'; binary.mkdir(); (binary / 'codex').write_bytes(b'synthetic binary')
    cfg = dict(schema='ctxpress.eval', version=1, scope='benchmark', start_mode='task_start', benchmark=name,
        model='main', reasoning='medium', backend='codex_docker', repeats=repeats, tasks=[t['id'] for t in selected],
        methods=[dict(label=label, **{'class':'NoCompaction'}) for label in ('reference','candidate')],
        environment=dict(data=str(root), resources=str(resources), bindir=str(binary)))
    if prices:
        cfg['prices'] = {'models':{'main':rates(), 'reflect':rates(.1)}}
    plan = eval_plan.compile_plan(cfg); directory = tmp_path / 'run'
    frozen = directory / 'runtime/ctxpress'; frozen.mkdir(parents=True); (frozen / 'fixture.py').write_text('# synthetic\n', encoding='utf-8')
    plan['code_sha256'] = eval_plan.fingerprint(frozen); plan['missing_environment_files'] = []
    plan['sha256'] = eval_task_compare._digest({k:v for k,v in plan.items() if k!='sha256'})
    write(directory / 'plan.json', plan); paths = eval_inputs.prepare(plan, directory / 'inputs'); jobs = []
    for spec in plan['jobs']:
        folder = directory / 'jobs' / spec['id'] / 'attempt-1'; folder.mkdir(parents=True)
        moved = eval_task_compare.task_api.remap(spec['task'], paths)
        task_dir = next(Path(i['path']).parent for i in moved['inputs'] if Path(i['path']).name == 'task.toml')
        project = 'ctxp-hb-' + hashlib.sha256(spec['id'].encode()).hexdigest()[:24]
        grader = 'ctxp-hb-' + hashlib.sha256((project + (':pro-regrade' if pro else ':verifier')).encode()).hexdigest()[:24]
        request = dict(schema='ctxpress.eval.harbor_trial', version=1, benchmark=name, task_id=moved['id'], task=str(task_dir),
            project=project, label=plan['sha256'][:12] + '-' + spec['id'], runtime=runtime,
            package=str(directory / 'runtime'), official=str(directory / 'inputs/official-inputs'), folder=str(folder),
            bindir=str(directory / 'inputs/bin'), profiles=str(folder / 'method-inputs'), method=spec['method'],
            model='main', reasoning='medium', compact_limit=spec['compact_limit'], run=plan['config']['run'],
            binary_version='synthetic-CLI', images={'main':images['agent']['id']}, grading_image=images['grading']['verifier']['id'])
        if deep:
            request['framework'] = 'pier'
        elif variant != 'legacy':
            request.update(harbor_api='modern', separate_verifier=not pro and variant != 'shared')
            if request['separate_verifier']:
                request['verifier_bundled_tests'] = True
        if pro:
            request.update(pro_version='v2', pro_base_commit=spec['task']['initial_state']['base_commit'])
        write(folder / 'harbor-request.json', request)
        trial_root = folder / project
        logs = trial_root / 'agent/ctxpress-requests.jsonl'; logs.parent.mkdir(parents=True)
        rows = [dict(request=1, status=200, model='main', usage=dict(input_tokens=120, cached_tokens=20, cache_write_tokens=10, output_tokens=5))]
        logs.write_text(''.join(json.dumps(r)+'\n' for r in rows), encoding='utf-8')
        rewards = dict(reward=1.0 if spec['label']=='reference' else 0.0,
                       partial=.8 if spec['label']=='reference' else .4)
        artifact_paths = [write(trial_root / 'artifacts/manifest.json', [])]
        if deep:
            patch = trial_root / 'artifacts/model.patch'; patch.write_text('synthetic patch\n', encoding='utf-8'); artifact_paths.append(patch)
            staging = trial_root / 'submission-0'; staging.mkdir(); shutil.copyfile(patch, staging / patch.name)
        module = 'pier_trial' if deep else 'harbor_modern' if variant != 'legacy' else 'harbor_worker'
        def config(phase, replay=False):
            mounts = [] if replay else [dict(type='bind', source=source, target=target, read_only=True, bind={'create_host_path':False})
                for source,target in [(request['package'],'/ctxpress-runtime'), (request['bindir'],'/cxbin'),
                                      (request['profiles'],'/ctxpress-method'), ('/tmp/synthetic-channel','/ctxpress-channel')]]
            if deep:
                mounts += [dict(type='bind', source=str(trial_root / n), target='/logs/'+n, bind={'create_host_path':False}) for n in ('agent','artifacts')]
            return dict(task={'path':request['task']}, trial_name=phase, trials_dir=str(folder), timeout_multiplier=1.0,
                agent=dict(import_path='ctxpress.harness.'+module+':CtxpressCodex', model_name='replay' if replay else 'main',
                           override_timeout_sec=300 if replay else plan['config']['run']['timeout']),
                environment=dict(import_path='ctxpress.harness.'+module+':CtxpressDocker', force_build=False, delete=True, mounts=mounts),
                verifier={'disable':pro and not replay})
        def official(phase, replay=False):
            raw = dict(task_name=moved['initial_state'].get('pier_task_name',moved['initial_state'].get('official_task_name',moved['id'])),
                trial_name=phase, trial_uri=(folder / phase).as_uri(), task_id={'path':request['task']},
                config=config(phase,replay), started_at='synthetic-start', finished_at='synthetic-finish', exception_info=None,
                agent_info=dict(name='patch-replay-agent' if replay else 'codex-locked' if pro else 'codex',
                    version='1.0.0' if replay else request['binary_version'],
                    model_info={'name':'replay' if replay else request['model'], 'provider':None if replay else 'openai'}),
                verifier_result=None if pro and not replay else {'rewards':rewards}, verifier_environment_mode='separate')
            write(folder / phase / 'config.json', raw['config'])
            return write(folder / phase / 'result.json', raw)
        primary = official(project); report = official(grader,True) if pro else primary
        score_root = folder / grader if pro else trial_root
        reward_file = write(score_root / 'verifier/reward.json', rewards)
        artifact_paths.append(reward_file)
        def record(p):return dict(path=str(p), sha256=eval_plan.file_sha256(p))
        limits = moved['initial_state'].get('verifier_environment',moved['initial_state']['environment'])
        def owner(phase, role, image, compose_root):
            allocation = limits if role=='verifier' else moved['initial_state']['environment']
            compose = write(compose_root / ('ctxpress-compose-agent.json' if pro else 'ctxpress-compose-'+role+'.json'),
                {'services':{'main':dict(image=image, **({'cpus':allocation['cpus']} if 'cpus' in allocation else {}),
                    **({'mem_limit':str(allocation['memory_mb']*1024*1024)} if 'memory_mb' in allocation else {}))}})
            if deep:
                native=json.loads(compose.read_text(encoding='utf-8'));main=native['services']['main']
                main['deploy']={'resources':{'limits':{'cpus':main.pop('cpus'),'memory':main.pop('mem_limit')}}}
                write(compose,native)
            row = dict(schema='ctxpress.eval.harbor_resources', version=1, project=phase, label=request['label'], images={'main':image},
                role=role, daemon_id='synthetic-daemon', cleaned=True, phase='stopped', credentials_may_exist=False,
                channel=None if role=='verifier' else '/tmp/synthetic-channel', compose_sha256=eval_plan.file_sha256(compose))
            if pro:row['pro_base_commit'] = request['pro_base_commit']
            return write(folder / ('resources-harbor-'+phase+'.json'), row)
        agent_owner = owner(project,'agent',images['agent']['id'],trial_root)
        verifier_owner = owner(grader,'verifier',images['grading']['verifier']['id'],score_root if pro else trial_root)
        artifacts = {str(p.relative_to(folder) if pro and p.is_relative_to(score_root) else p.relative_to(trial_root)):record(p) for p in artifact_paths}
        separation = dict(agent_project=project, verifier_project=grader, agent_image=images['agent']['id'],
            verifier_image=images['grading']['verifier']['id'], checked_cleanup=True, author_collect=True)
        separation.update(patch_only_transfer=True) if deep else separation.update(author_artifact_handler=True, verifier_started=True, bundled_tests=True)
        state = dict(stop='completed', calls=1, official_artifacts=artifacts, separate_verifier=separation)
        if pro:
            capture = trial_root / 'agent/model.patch'; capture.write_text('synthetic captured patch\n', encoding='utf-8')
            submission = folder / 'pro-submission/model.patch'; submission.parent.mkdir(); shutil.copyfile(capture,submission)
            fresh = dict(schema='ctxpress.eval.pro_regrade', version=1, task_id=moved['id'], benchmark_version='v2',
                base_commit=request['pro_base_commit'], agent_project=project, regrade_project=grader,
                agent_image=images['agent']['id'], regrade_image=images['grading']['verifier']['id'], agent_report=record(primary),
                regrade_report=record(report), submission=record(submission), checked_agent_cleanup=True, regrade_model_calls=0,
                published_protocol_reproduced=False, agent_resources_sha256=eval_plan.file_sha256(agent_owner),
                regrade_resources_sha256=eval_plan.file_sha256(verifier_owner))
            fresh_file = write(folder / 'pro-regrade.json',fresh)
            artifacts.update({'pro-regrade.json':record(fresh_file), 'pro-submission/model.patch':record(submission)})
            state.update(fresh_regrade=fresh, pro_version='v2', agent_report=record(primary), regrade_report=record(report), submission=record(submission))
            separation.update(author_patch_replay=True)
        write(folder / 'harbor-worker-result.json',state)
        grade = benchmarks.get(name).read_grade(moved,report)
        assert grade.get('valid_rewards'),grade
        result = dict(task=moved, method=spec['method'], model='main', reasoning='medium', benchmark={'name':name},
            protocol='ctxpress_comparison', requests=1, rewrites=rows, proxy_log=str(logs),
            usage={'summary_calls':0,'native_compaction_calls':0}, grade=grade, official_report=str(report),
            binary_version=request['binary_version'], **state)
        write(folder / 'result.json',result)
        jobs.append(dict(id=spec['id'],spec=json.dumps(spec),status='completed',attempt=1,result=json.dumps(result)))
    return plan,jobs,directory


@pytest.mark.linux_only
@pytest.mark.parametrize('name',['terminal-bench','terminal-bench-science','deep-swe','swe-bench-pro'])
def test_native_named_metrics_have_independent_deltas_and_versions(tmp_path,name):
    out = run(fixture(tmp_path,name)); pair = out['candidates'][0]['pairs'][0]
    assert pair['quality_admissible'] and pair['api_cost_complete']
    assert pair['metrics']['rewards.reward'] == dict(reference=1.0,candidate=0.0,delta=-1.0,comparable=True)
    assert pair['metrics']['rewards.partial']['delta'] == pytest.approx(-.4)
    assert out['metric_names'] == ['rewards.partial','rewards.reward'] and 'resolved' not in pair['metrics']
    details = pair['candidate']['grade_details']; assert details['versions']['version'] == ('0.3.1' if name=='deep-swe' else '0.23.0')
    if name=='swe-bench-pro':
        assert details['authoritative_phase']=='fresh_regrade' and details['published_protocol_reproduced'] is False
    json.dumps(out,allow_nan=False)


@pytest.mark.linux_only
def test_task_and_repeat_coordinates_do_not_pool_or_cross_pair(tmp_path):
    data = fixture(tmp_path,repeats=2,tasks=2); out=run(data); pairs=out['candidates'][0]['pairs']
    assert {(p['task_id'],p['repeat']) for p in pairs}=={(t,r) for t in ('task-one','task-two') for r in (0,1)}
    jobs=data[1]; missing=next(j for j in jobs if json.loads(j['spec'])['repeat']==1 and json.loads(j['spec'])['task']['id']=='task-two' and json.loads(j['spec'])['label']=='candidate');jobs.remove(missing)
    out=run(data);assert out['candidates'][0]['coverage']['comparable_pairs']==3
    assert next(p for p in out['candidates'][0]['pairs'] if p['task_id']=='task-two' and p['repeat']==1)['metrics']['rewards.reward']['candidate'] is None


@pytest.mark.linux_only
def test_prices_missing_does_not_invalidate_official_rewards(tmp_path):
    out=run(fixture(tmp_path,prices=False));pair=out['candidates'][0]['pairs'][0]
    assert pair['quality_admissible'] and not pair['api_cost_complete'] and pair['candidate_api_cost_usd'] is None


@pytest.mark.parametrize('variant',['shared','legacy'])
def test_variant_scope_is_explicit_and_observed_cost_retained(tmp_path,variant):
    out=run(fixture(tmp_path,variant=variant));row=out['jobs'][0]
    assert out['supported'] and row['comparison_variant_supported'] is False and row['unsupported_reason']
    assert not row['quality_admissible'] and row['api_cost_complete']


@pytest.mark.linux_only
@pytest.mark.parametrize('fault',['missing_reward','reward_drift','report_drift','artifact_added','request_image','request_model',
    'request_task','official_task','official_config','cleanup','compose_drift','compose_image','compose_cpu','boolean_reward','test_only'])
def test_evidence_faults_reject_quality_without_fabricating_metrics(tmp_path,fault):
    data=fixture(tmp_path);plan,jobs,directory=data;job=jobs[1];folder=directory/'jobs'/job['id']/'attempt-1'
    result=json.loads(job['result']);request=json.loads((folder/'harbor-request.json').read_text(encoding='utf-8'));root=folder/request['project']
    if fault=='missing_reward':(root/'verifier/reward.json').unlink()
    if fault=='reward_drift':write(root/'verifier/reward.json',{'reward':1})
    if fault=='report_drift':p=root/'result.json';p.write_text(p.read_text(encoding='utf-8')+' ', encoding='utf-8')
    if fault=='artifact_added':write(root/'verifier/unrecorded.json',{})
    if fault in ('request_image','request_model','request_task'):
        request.update({'images':{'main':'sha256:'+'9'*64}} if fault=='request_image' else {'model':'other'} if fault=='request_model' else {'task':str(root)})
        write(folder/'harbor-request.json',request)
    if fault in ('official_task','official_config'):
        p=root/'result.json';raw=json.loads(p.read_text(encoding='utf-8'))
        if fault=='official_task':raw['task_name']='publisher/other'
        else:raw['config']['agent']['override_timeout_sec']=999
        write(p,raw);change_result(job,directory,lambda r:r['grade'].update(report_sha256=eval_plan.file_sha256(p)))
    if fault in ('cleanup','compose_drift','compose_image','compose_cpu'):
        owner=folder/('resources-harbor-'+request['project']+'.json');row=json.loads(owner.read_text(encoding='utf-8'));compose=root/'ctxpress-compose-agent.json'
        if fault=='cleanup':row['cleaned']=False;write(owner,row)
        else:
            raw=json.loads(compose.read_text(encoding='utf-8'));raw['services']['main']['image']='sha256:'+'9'*64 if fault=='compose_image' else raw['services']['main']['image']
            if fault=='compose_cpu':raw['services']['main']['cpus']=8
            write(compose,raw)
            if fault=='compose_drift':compose.write_text(compose.read_text(encoding='utf-8')+' ', encoding='utf-8')
            if fault!='compose_drift':row['compose_sha256']=eval_plan.file_sha256(compose);write(owner,row)
    if fault=='boolean_reward':change_result(job,directory,lambda r:r['grade']['rewards'].update(reward=True))
    if fault=='test_only':change_result(job,directory,lambda r:r.update(test_only=True))
    out=run(data);row=out['jobs'][1]
    assert not row['quality_admissible'] and row['metrics']=={} and (row['reasons'] or row['quality_reasons'])
    assert out['candidates'][0]['pairs'][0]['metrics']['rewards.reward']['delta'] is None
    if fault not in ('request_image','request_model','request_task','test_only'):assert row['api_cost_complete']


def test_tool_spawn_failure_is_diagnostic_quality_with_observed_cost(tmp_path):
    data=fixture(tmp_path);plan,jobs,directory=data;job=jobs[1];value=json.loads(job['result'])
    session=Path(value['proxy_log']).parent/'sessions/rollout-fixture.jsonl';session.parent.mkdir()
    session.write_text('\n'.join(json.dumps(x) for x in [
        dict(type='response_item',payload=dict(type='custom_tool_call',name='exec',call_id='a')),
        dict(type='response_item',payload=dict(type='custom_tool_call_output',call_id='a',output='failed to spawn code-mode host /cxbin/codex-code-mode-host: missing executable'))]), encoding='utf-8')
    out=run(data);row=out['jobs'][1]
    assert row['execution_health']['execution_invalid'] and not row['quality_admissible'] and row['api_cost_usd']>0


@pytest.mark.linux_only
def test_pro_locked_author_name_and_replay_identity_match_real_report_shape(tmp_path):
    data = fixture(tmp_path, 'swe-bench-pro')
    plan, jobs, directory = data
    source = directory / 'inputs/official-inputs/pro_tooling/locked_codex.py'
    out = run(data)
    for job, row in zip(jobs, out['jobs']):
        result = json.loads(job['result'])
        primary = json.loads(Path(result['agent_report']['path']).read_text(encoding='utf-8'))
        replay = json.loads(Path(result['regrade_report']['path']).read_text(encoding='utf-8'))
        assert primary['agent_info'] == dict(name='codex-locked', version=result['binary_version'],
            model_info={'name':plan['config']['model'], 'provider':'openai'})
        assert replay['agent_info'] == dict(name='patch-replay-agent', version='1.0.0',
            model_info={'name':'replay', 'provider':None})
        assert row['quality_admissible'] and row['api_cost_complete']
        assert row['grade_details']['authoritative_phase'] == 'fresh_regrade'
        assert any(a['path'] == str(source) and a['sha256'] == eval_plan.file_sha256(source) for a in row['artifacts'])
    assert out['candidates'][0]['pairs'][0]['metrics']['rewards.reward']['delta'] == -1


@pytest.mark.linux_only
@pytest.mark.parametrize('fault', ['plain_codex', 'other_agent', 'model', 'binary', 'config_model', 'config_import', 'report_hash'])
def test_pro_locked_identity_cannot_bypass_model_binary_config_or_report_receipts(tmp_path, fault):
    data = fixture(tmp_path, 'swe-bench-pro'); plan, jobs, directory = data
    job = jobs[1]; folder = directory / 'jobs' / job['id'] / 'attempt-1'
    record = json.loads((folder / 'pro-regrade.json').read_text(encoding='utf-8'))
    primary = Path(record['agent_report']['path']); raw = json.loads(primary.read_text(encoding='utf-8'))
    if fault == 'plain_codex': raw['agent_info']['name'] = 'codex'
    if fault == 'other_agent': raw['agent_info']['name'] = 'other-agent'
    if fault == 'model': raw['agent_info']['model_info']['name'] = 'other-model'
    if fault == 'binary': raw['agent_info']['version'] = 'other-binary'
    if fault == 'config_model': raw['config']['agent']['model_name'] = 'other-model'
    if fault == 'config_import': raw['config']['agent']['import_path'] = 'other:Agent'
    write(primary, raw)
    if fault == 'report_hash':
        primary.write_text(primary.read_text(encoding='utf-8') + ' ', encoding='utf-8')
    else:
        # Keep all receipts consistent so identity failures cannot pass merely
        # because an earlier stale-hash check rejected the edited report.
        record['agent_report']['sha256'] = eval_plan.file_sha256(primary)
        fresh = write(folder / 'pro-regrade.json', record)
        worker = json.loads((folder / 'harbor-worker-result.json').read_text(encoding='utf-8'))
        worker.update(agent_report=record['agent_report'], fresh_regrade=record)
        worker['official_artifacts']['pro-regrade.json']['sha256'] = eval_plan.file_sha256(fresh)
        write(folder / 'harbor-worker-result.json', worker)
        def refresh(result):
            result.update(agent_report=record['agent_report'], fresh_regrade=record, official_artifacts=worker['official_artifacts'])
            result['grade']['fresh_regrade'] = dict(record, path=str(fresh), sha256=eval_plan.file_sha256(fresh))
        change_result(job, directory, refresh)
    row = run(data)['jobs'][1]
    assert not row['quality_admissible'] and row['metrics'] == {} and row['api_cost_complete']
    reason = 'hash changed' if fault == 'report_hash' else 'config differs' if fault.startswith('config_') else 'Agent model/binary identity differs'
    assert any(reason in item for item in row['quality_reasons'])


@pytest.mark.linux_only
def test_pro_name_is_bound_to_frozen_tooling_not_original_checkout(tmp_path):
    data = fixture(tmp_path, 'swe-bench-pro')
    original = tmp_path / 'official/pro_tooling/locked_codex.py'
    original.write_text(LOCKED_CODEX_SOURCE.replace('codex-locked', 'other-agent'), encoding='utf-8')
    assert all(row['quality_admissible'] for row in run(data)['jobs'])
    frozen = data[2] / 'inputs/official-inputs/pro_tooling/locked_codex.py'
    frozen.write_text(original.read_text(encoding='utf-8'), encoding='utf-8')
    out = run(data)
    assert all(not row['quality_admissible'] and not row['metrics'] for row in out['jobs'])
    assert all(any('unverified frozen run' in reason for reason in row['reasons']) for row in out['jobs'])


def test_pro_dynamic_author_name_is_explicitly_unsupported_without_executing_source(tmp_path):
    from ctxpress.harness.results import harbor_compare as eval_harbor_compare
    root = tmp_path / 'official'; source = root / 'pro_tooling/locked_codex.py'
    source.parent.mkdir(parents=True)
    source.write_text(LOCKED_CODEX_SOURCE.replace('return "codex-locked"', 'return resolve_name()') +
                      '\nraise AssertionError("author source must never execute")\n', encoding='utf-8')
    evidence = []
    with pytest.raises(eval_harbor_compare.UnsupportedVariant, match='static literal author name'):
        eval_harbor_compare._pro_agent_name({'official':str(root)}, evidence)
    assert evidence[0]['sha256'] == eval_plan.file_sha256(source)


@pytest.mark.parametrize('fault',['source_patch','fresh_record','base_commit','replay_usage','replay_owner','replay_reward','replay_config'])
def test_pro_v2_requires_exact_official_capture_and_fresh_regrade(tmp_path,fault):
    data=fixture(tmp_path,'swe-bench-pro');plan,jobs,directory=data;job=jobs[1];folder=directory/'jobs'/job['id']/'attempt-1'
    request=json.loads((folder/'harbor-request.json').read_text(encoding='utf-8'));record=json.loads((folder/'pro-regrade.json').read_text(encoding='utf-8'));root=folder/request['project'];replay=folder/record['regrade_project']
    if fault=='source_patch':(root/'agent/model.patch').write_text('changed capture', encoding='utf-8')
    if fault=='fresh_record':(folder/'pro-regrade.json').unlink()
    if fault=='base_commit':request['pro_base_commit']='0'*40;write(folder/'harbor-request.json',request)
    if fault in ('replay_usage','replay_config'):
        p=replay/'result.json';raw=json.loads(p.read_text(encoding='utf-8'))
        if fault=='replay_usage':raw['agent_result']={'n_input_tokens':4}
        else:raw['config']['agent']['model_name']='main'
        write(p,raw);change_result(job,directory,lambda r:r['grade'].update(report_sha256=eval_plan.file_sha256(p)))
    if fault=='replay_owner':p=folder/('resources-harbor-'+record['regrade_project']+'.json');v=json.loads(p.read_text(encoding='utf-8'));v['cleaned']=False;write(p,v)
    if fault=='replay_reward':write(replay/'verifier/reward.json',{'reward':1})
    if fault in ('replay_usage','replay_config'):
        # Rebind every receipt: these cases must reject contradictory runtime
        # semantics even when all freshly recorded artifact hashes agree.
        digest=eval_plan.file_sha256(replay/'result.json');record['regrade_report']['sha256']=digest
        write(folder/'pro-regrade.json',record)
        worker=json.loads((folder/'harbor-worker-result.json').read_text(encoding='utf-8'))
        worker['fresh_regrade']=record;worker['regrade_report']=record['regrade_report']
        worker['official_artifacts']['pro-regrade.json']['sha256']=eval_plan.file_sha256(folder/'pro-regrade.json')
        write(folder/'harbor-worker-result.json',worker)
        def refresh(r):
            r.update(fresh_regrade=record,regrade_report=record['regrade_report'],official_artifacts=worker['official_artifacts'])
            r['grade'].update(report_sha256=digest,fresh_regrade=dict(record,path=str(folder/'pro-regrade.json'),sha256=eval_plan.file_sha256(folder/'pro-regrade.json')))
        change_result(job,directory,refresh)
    out=run(data);row=out['jobs'][1]
    assert not row['quality_admissible'] and row['metrics']=={}
    if fault!='base_commit':assert row['api_cost_complete']


def replace_rewards(data, job, rewards):
    plan,jobs,directory=data;folder=directory/'jobs'/job['id']/'attempt-1';value=json.loads(job['result']);report=Path(value['official_report']);raw=json.loads(report.read_text(encoding='utf-8'))
    raw['verifier_result']['rewards']=rewards;write(report,raw)
    reward=report.parent/'verifier/reward.json';write(reward,rewards);worker=json.loads((folder/'harbor-worker-result.json').read_text(encoding='utf-8'));worker['official_artifacts']['verifier/reward.json']['sha256']=eval_plan.file_sha256(reward);write(folder/'harbor-worker-result.json',worker)
    def change(r):r['grade'].update(rewards=rewards,report_sha256=eval_plan.file_sha256(report));r['official_artifacts']=worker['official_artifacts']
    change_result(job,directory,change)


@pytest.mark.linux_only
def test_named_reward_schema_drift_prevents_pair_and_never_imputes(tmp_path):
    data=fixture(tmp_path);replace_rewards(data,data[1][1],{'reward':0.0,'another_metric':.2})
    out=run(data);pair=out['candidates'][0]['pairs'][0]
    assert all(j['quality_admissible'] for j in out['jobs']) and not pair['quality_admissible']
    assert pair['metrics']['rewards.partial']['candidate'] is None and pair['metrics']['rewards.partial']['delta'] is None
    assert any('metric names differ' in reason for reason in pair['reasons'])


@pytest.mark.linux_only
def test_fractional_and_negative_author_rewards_are_not_booleanized(tmp_path):
    data=fixture(tmp_path);replace_rewards(data,data[1][0],{'reward':2.5,'partial':-.25})
    replace_rewards(data,data[1][1],{'reward':1.2,'partial':.75})
    pair=run(data)['candidates'][0]['pairs'][0]
    assert pair['quality_admissible'] and pair['metrics']['rewards.reward']['delta']==pytest.approx(-1.3)
    assert pair['metrics']['rewards.partial']['delta']==1.0


@pytest.mark.parametrize('fault',['missing','running','failed','retry','duplicate','changedspec'])
def test_harbor_incomplete_pairing_uses_the_common_frozen_plan_gates(tmp_path,fault):
    data=fixture(tmp_path);plan,jobs,directory=data;job=jobs[1]
    if fault=='missing':jobs.pop()
    if fault in ('running','failed'):job['status']=fault
    if fault=='retry':job['attempt']=2
    if fault=='duplicate':jobs.append(copy.deepcopy(job))
    if fault=='changedspec':value=json.loads(job['spec']);value['repeat']=999;job['spec']=json.dumps(value)
    pair=run(data)['candidates'][0]['pairs'][0]
    assert not pair['quality_admissible'] and not pair['api_cost_complete'] and pair['api_cost_delta_usd'] is None


@pytest.mark.linux_only
def test_cache_write_unknown_keeps_cost_null_and_quality_independent(tmp_path):
    data=fixture(tmp_path);plan,jobs,directory=data
    def change(r):
        r['rewrites'][0]['usage'].pop('cache_write_tokens')
        Path(r['proxy_log']).write_text(''.join(json.dumps(x)+'\n' for x in r['rewrites']), encoding='utf-8')
    change_result(jobs[1],directory,change);pair=run(data)['candidates'][0]['pairs'][0]
    assert pair['quality_admissible'] and not pair['api_cost_complete'] and pair['candidate_api_cost_usd'] is None


@pytest.mark.linux_only
def test_summary_model_overhead_is_billed_independently_from_native_rewards(tmp_path):
    data=fixture(tmp_path);plan,jobs,directory=data
    def change(r):
        r['rewrites'].append(dict(type='summary',purpose='reflect',model='reflect',status=200,completed=True,
            usage=dict(input_tokens=200,cached_tokens=100,cache_write_tokens=20,output_tokens=10)))
        r['usage']['summary_calls']=1
        Path(r['proxy_log']).write_text(''.join(json.dumps(x)+'\n' for x in r['rewrites']), encoding='utf-8')
    change_result(jobs[1],directory,change)
    out=run(data);pair=out['candidates'][0]['pairs'][0]
    assert pair['quality_admissible'] and pair['api_cost_complete']
    assert pair['candidate']['usage']['summary_calls']==1 and 'reflect' in pair['candidate']['bill']['usage_by_model']
    assert pair['api_cost_delta_usd']>0


@pytest.mark.parametrize('fault',['request','grade','original_report'])
def test_non_model_preflight_and_test_only_evidence_never_enter_quality(tmp_path,fault):
    data=fixture(tmp_path,'deep-swe');plan,jobs,directory=data;folder=directory/'jobs'/jobs[1]['id']/'attempt-1'
    if fault=='request':
        p=folder/'harbor-request.json';value=json.loads(p.read_text(encoding='utf-8'));value.update(model=None,test_only=True);write(p,value)
    elif fault=='grade':change_result(jobs[1],directory,lambda r:r['grade'].update(test_only=True))
    else:
        value=json.loads(jobs[1]['result']);p=Path(value['official_report']);raw=json.loads(p.read_text(encoding='utf-8'));raw['test_only']=True;write(p,raw)
        change_result(jobs[1],directory,lambda r:r['grade'].update(report_sha256=eval_plan.file_sha256(p)))
    row=run(data)['jobs'][1];assert not row['quality_admissible'] and not row['metrics']


@pytest.mark.linux_only
def test_analysis_is_read_only_and_never_launches_network_or_processes(tmp_path,monkeypatch):
    import socket
    import subprocess
    data=fixture(tmp_path,'deep-swe');directory=data[2]
    before={str(p.relative_to(directory)):p.read_bytes() for p in directory.rglob('*') if p.is_file()}
    def forbidden(*args,**kwargs):raise AssertionError('analysis attempted external execution')
    monkeypatch.setattr(subprocess,'Popen',forbidden)
    monkeypatch.setattr(subprocess,'run',forbidden)
    monkeypatch.setattr(socket,'create_connection',forbidden)
    out=run(data)
    assert out['candidates'][0]['quality_complete']
    assert before=={str(p.relative_to(directory)):p.read_bytes() for p in directory.rglob('*') if p.is_file()}
    encoded=json.dumps(out);assert 'rewrites' not in encoded and 'instruction.md' not in encoded


@pytest.mark.linux_only
def test_pier_compose_equivalent_limits_cannot_hide_a_conflicting_second_limit(tmp_path):
    data=fixture(tmp_path,'deep-swe');plan,jobs,directory=data;folder=directory/'jobs'/jobs[1]['id']/'attempt-1'
    request=json.loads((folder/'harbor-request.json').read_text(encoding='utf-8'));compose=folder/request['project']/'ctxpress-compose-agent.json'
    raw=json.loads(compose.read_text(encoding='utf-8'));raw['services']['main']['cpus']=8;write(compose,raw)
    owner=folder/('resources-harbor-'+request['project']+'.json');row=json.loads(owner.read_text(encoding='utf-8'));row['compose_sha256']=eval_plan.file_sha256(compose);write(owner,row)
    out=run(data);assessment=out['jobs'][1]
    assert not assessment['quality_admissible'] and assessment['api_cost_complete']
    assert any('CPU/memory differs' in reason for reason in assessment['quality_reasons'])
