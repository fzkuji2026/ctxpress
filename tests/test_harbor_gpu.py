"""GPU protocol contracts; no real GPU, model, installer or Docker daemon."""
import asyncio, json
from types import SimpleNamespace
import pytest
from ctxpress import benchmarks
from ctxpress.benchmarks.harbor import driver as harbor_driver, gpu as harbor_gpu
from ctxpress.benchmarks.harbor.environment import framework, guarded_compose
from ctxpress.harness.jobs import environment as eval_environment, resources as task_resources
from test_harbor_environment import OfficialFixture, model, settings
from test_task_resources import capture_spec

A='GPU-'+'a'*8+'-'+'a'*4+'-'+'a'*4+'-'+'a'*4+'-'+'a'*12
B='GPU-'+'b'*8+'-'+'b'*4+'-'+'b'*4+'-'+'b'*4+'-'+'b'*12
QUERY=f'NVIDIA H100 80GB HBM3, {A}, 81559, 550.54.15\n'


def test_gpu_binding_preserves_limits_and_never_grants_devices_to_auxiliary_services(tmp_path):
    original=model(tmp_path);cfg=settings(tmp_path);cfg['gpu_device_ids']=[A]
    original['services']['main']['environment']={'NVIDIA_VISIBLE_DEVICES':'all','TASK_SETTING':'preserved'}
    original['services']['main']['deploy']['resources']['reservations']={'memory':'1G','devices':[
        dict(driver='nvidia',count=1,capabilities=['gpu'])]}
    value=guarded_compose(original,**cfg);main=value['services']['main'];database=value['services']['db']
    assert main['deploy']['resources']['limits']==original['services']['main']['deploy']['resources']['limits']
    assert main['deploy']['resources']['reservations']==dict(memory='1G',devices=[dict(driver='nvidia',device_ids=[A],capabilities=['gpu'])])
    assert main['environment']=={'NVIDIA_VISIBLE_DEVICES':A,'TASK_SETTING':'preserved'}
    assert database['environment']['NVIDIA_VISIBLE_DEVICES']=='void'
    assert main['labels']['ctxpress.gpu.devices']==A and database['labels']['ctxpress.gpu.devices']==''
    assert original['services']['main']['deploy']['resources']['reservations']['devices'][0]['count']==1


@pytest.mark.parametrize('change',['aux-gpu','unbounded','wrong-count','other-device','host-device','driver-options'])
def test_conflicting_gpu_requests_fail_before_any_start(tmp_path,change):
    value=model(tmp_path);cfg=settings(tmp_path);cfg['gpu_device_ids']=[A];main=value['services']['main']
    declaration=dict(driver='nvidia',count=1,capabilities=['gpu'])
    if change=='aux-gpu':value['services']['db']['gpus']='all'
    if change=='unbounded':main['gpus']='all'
    if change=='wrong-count':declaration['count']=2
    if change=='other-device':declaration.pop('count');declaration['device_ids']=[B]
    if change=='host-device':main['devices']=['/dev/nvidia0:/dev/nvidia0']
    if change=='driver-options':declaration['options']={'undeclared':'true'}
    main['deploy']['resources']['reservations']={'devices':[declaration]}
    with pytest.raises(ValueError):guarded_compose(value,**cfg)


@pytest.mark.parametrize('field',['gpus','runtime','deploy'])
def test_cpu_tasks_cannot_gain_gpu_devices_from_task_compose(tmp_path,field):
    value=model(tmp_path);main=value['services']['main']
    if field=='gpus':main['gpus']='all'
    if field=='runtime':main['runtime']='nvidia'
    if field=='deploy':main['deploy']['resources']['reservations']={'devices':[{'capabilities':['gpu']}]}
    with pytest.raises(ValueError,match='device binding'):guarded_compose(value,**settings(tmp_path))


def test_actual_device_query_preserves_hardware_and_accepts_exact_model_families():
    evidence=harbor_gpu.observed(QUERY,[A],['A100','H100'])
    assert evidence['count']==1 and evidence['devices'][0]==dict(name='NVIDIA H100 80GB HBM3',uuid=A,memory_mb=81559,driver_version='550.54.15')
    assert harbor_gpu.matches('NVIDIA A100-SXM4-80GB',['A100'])
    assert not harbor_gpu.matches('NVIDIA H1000',['H100'])
    assert not harbor_gpu.matches('NVIDIA L40S',['L40'])
    assert harbor_gpu.matches('NVIDIA A100-SXM4-80GB',['A100-80GB'])
    assert harbor_gpu.matches('NVIDIA A100-PCIE-80GB',['A100-80GB'])
    assert not harbor_gpu.matches('NVIDIA A100-PCIE-40GB',['A100-80GB'])
    assert harbor_gpu.requirements({'gpus':1,'gpu_types':[]})=={'count':1,'types':None}


@pytest.mark.parametrize('output',[QUERY.replace(A,B),QUERY+QUERY,QUERY.replace('H100','T4'),
    QUERY.replace('81559','N/A'),QUERY.replace('550.54.15',''),QUERY.replace('GPU-','MIG-'),''])
def test_wrong_or_incomplete_hardware_never_satisfies_task_gpu_contract(output):
    with pytest.raises(ValueError):harbor_gpu.observed(output,[A],['H100'])


class GPUEnvironment(OfficialFixture):
    def __init__(self,folder,output=QUERY,**kw):
        super().__init__(folder,**kw)
        self.task_env_config.gpus=1;self.task_env_config.gpu_types=['H100']
        self.output=output;self.queries=[]
    async def exec(self,command,**kwargs):
        self.queries.append(command)
        return SimpleNamespace(return_code=0,stdout=self.output)


def test_gpu_runtime_is_checked_after_up_and_before_agent_can_start(tmp_path,monkeypatch):
    cfg=settings(tmp_path);cfg['gpu_device_ids']=[A];events=[]
    async def cleanup(environment):events.append('credentials')
    instance=framework(GPUEnvironment,SimpleNamespace,cfg,cleanup,journal=lambda phase,env:events.append(phase))(tmp_path)
    async def compose(args,**kw):
        events.append(args[0]);return SimpleNamespace(stdout=json.dumps(model(tmp_path)),return_code=0)
    instance._run_docker_compose_command=compose
    monkeypatch.setattr(eval_environment,'image',lambda reference:{'id':reference})
    asyncio.run(instance.start())
    assert instance.supports_gpus and events==['config','starting','up','running']
    assert instance.ctxpress_gpu_evidence['devices'][0]['uuid']==A
    assert instance.queries==['nvidia-smi --query-gpu=name,uuid,memory.total,driver_version --format=csv,noheader,nounits']
    asyncio.run(instance.stop());assert events[-3:]==['credentials','down','stopped']


def test_wrong_model_prevents_ready_state_and_preserves_owned_cleanup(tmp_path,monkeypatch):
    cfg=settings(tmp_path);cfg['gpu_device_ids']=[A];events=[]
    async def cleanup(environment):events.append('cleanup-without-agent')
    instance=framework(GPUEnvironment,SimpleNamespace,cfg,cleanup,journal=lambda phase,env:events.append(phase))(tmp_path,output=QUERY.replace('H100','T4'))
    async def compose(args,**kw):return SimpleNamespace(stdout=json.dumps(model(tmp_path)),return_code=0)
    instance._run_docker_compose_command=compose;monkeypatch.setattr(eval_environment,'image',lambda reference:{'id':reference})
    with pytest.raises(ValueError,match='gpu_types'):asyncio.run(instance.start())
    assert instance._ctxpress_started and instance.ctxpress_gpu_evidence is None and events==['starting']
    asyncio.run(instance.stop());assert events==['starting','cleanup-without-agent','stopped']


@pytest.mark.parametrize('devices',[None,[],['0'],['MIG-fixture'],[A,A],[A,B]])
def test_invalid_gpu_capture_is_rejected_before_docker_image_inspection(tmp_path,monkeypatch,devices):
    root,_,spec=capture_spec(tmp_path)
    (root/'task-one/task.toml').write_text('[environment]\ngpus=1\ngpu_types=["H100"]\n', encoding='utf-8')
    selected=benchmarks.get('terminal-bench').task_instances(root)
    if devices is not None:spec['tasks'][selected[0]['id']]['gpu_device_ids']=devices
    monkeypatch.setattr(eval_environment,'image',lambda reference:pytest.fail('inspected invalid GPU declaration'))
    with pytest.raises(ValueError,match='GPU UUIDs'):task_resources.capture(spec,selected)


def test_gpu_capture_pins_explicit_devices_without_discovery_or_container_execution(tmp_path,monkeypatch):
    root,_,spec=capture_spec(tmp_path)
    (root/'task-one/task.toml').write_text('[environment]\ngpus=1\ngpu_types=["H100"]\n', encoding='utf-8')
    selected=benchmarks.get('terminal-bench').task_instances(root)
    spec['tasks'][selected[0]['id']]['gpu_device_ids']=[A]
    monkeypatch.setattr(eval_environment,'image',lambda reference:{'id':'sha256:'+'f'*64})
    lock=task_resources.capture(spec,selected)
    assert lock['tasks'][selected[0]['id']]['gpu_device_ids']==[A]
    spec['tasks'][selected[0]['id']]['gpu_device_ids'].append(B)
    assert lock['tasks'][selected[0]['id']]['gpu_device_ids']==[A]


def test_retained_container_blocks_another_gpu_job_before_launch(monkeypatch):
    calls=[]
    def docker(*args):
        calls.append(args)
        if args[0]=='ps':return 'retained-fixture\n'
        return json.dumps([{'Config':{'Labels':{'ctxpress.gpu.devices':A}}}])
    monkeypatch.setattr(harbor_driver,'docker',docker)
    with pytest.raises(RuntimeError,match='retained'):harbor_driver.available_gpus([A])
    assert calls[0]==('ps','-aq','--filter','label=ctxpress.managed=true')
    harbor_driver.available_gpus([B])


@pytest.mark.parametrize('allocation',[{'Driver':'nvidia','DeviceIDs':[A]},
    {'Driver':'nvidia','DeviceIDs':['0']},{'Driver':'nvidia','Count':-1}])
def test_missing_gpu_labels_do_not_hide_retained_docker_allocations(monkeypatch,allocation):
    def docker(*args):
        if args[0]=='ps':return 'retained-fixture'
        return json.dumps([{'HostConfig':{'DeviceRequests':[allocation]},'Config':{'Labels':{}}}])
    monkeypatch.setattr(harbor_driver,'docker',docker)
    with pytest.raises(RuntimeError,match='retained'):harbor_driver.available_gpus([A])


def test_reports_preserve_gpu_binding_and_observation_for_success_and_missing_evidence(tmp_path):
    from ctxpress.harness.jobs import plan as eval_plan, queue as evaluation
    from ctxpress.harness.results.report import report, write_report
    from test_harbor_driver import prepared
    _,plan,_,_,_,job,_=prepared(tmp_path)
    plan={key:value for key,value in plan.items() if key!='sha256'}
    task=job['task'];path=task['initial_state']['task_directory']+'/task.toml'
    from pathlib import Path
    Path(path).write_text('[environment]\ngpus=1\ngpu_types=["H100"]\n', encoding='utf-8')
    # Re-discover and compile rather than editing a sealed task/hash binding.
    found=benchmarks.get('terminal-bench').task_instances(plan['config']['environment']['data'])[0]
    resources=plan['config']['environment']['resources']
    lock=json.loads(Path(resources).read_text(encoding='utf-8'));content={key:value for key,value in lock.items() if key!='sha256'}
    content['tasks'][found['id']]['task_sha256']=task_resources.task_digest(found)
    content['tasks'][found['id']]['gpu_device_ids']=[A]
    Path(resources).write_text(json.dumps(task_resources.seal(content)), encoding='utf-8')
    config=plan['config'];config['repeats']=2
    revised=eval_plan.compile_plan(config);directory=evaluation.prepare(revised,tmp_path/'gpu-report')
    observed=harbor_gpu.observed(QUERY,[A],['H100'])
    with evaluation.database(directory) as connection:
        result=dict(test_only=True,gpu=observed,requests=0,rewrites=[],grade={})
        connection.execute("UPDATE jobs SET status='failed',result=? WHERE id=?",(json.dumps(result),revised['jobs'][0]['id']))
    result=report(directory);evidence=result['methods'][0]['jobs']
    assert evidence[0]['gpu']==observed and evidence[0]['gpu_device_ids']==[A]
    assert evidence[1]['gpu_device_ids']==[A] and not evidence[1].get('gpu')
    assert result['task_resource_manifest_sha256']==revised['task_resources']['sha256']
    write_report(directory);markup=(directory/'report.html').read_text(encoding='utf-8')
    assert 'GPU 运行环境' in markup and 'NVIDIA H100' in markup and A in markup and '未记录' in markup
    assert '未声明跨任务的镜像与需求目录清单' not in markup


def test_scheduler_queues_shared_gpu_without_blocking_cpu_or_disjoint_gpu_tasks(tmp_path):
    import shutil
    from pathlib import Path
    from ctxpress.harness.jobs import plan as eval_plan, queue as evaluation
    from test_harbor_driver import prepared
    from test_evaluation import fake_launch
    _,original,_,_,_,_,_=prepared(tmp_path)
    root=Path(original['config']['environment']['data']);first=root/'task-one'
    shutil.copytree(first,root/'task-cpu');shutil.copytree(first,root/'task-disjoint')
    for directory in (first,root/'task-disjoint'):
        (directory/'task.toml').write_text('[environment]\ngpus=1\ngpu_types=["H100"]\n', encoding='utf-8')
    found=benchmarks.get('terminal-bench').task_instances(root)
    resources=Path(original['config']['environment']['resources']);lock=json.loads(resources.read_text(encoding='utf-8'))
    content={key:value for key,value in lock.items() if key!='sha256'}
    images=content['tasks']['task-one']['images'];content['tasks']={}
    for item in found:
        record=dict(task_sha256=task_resources.task_digest(item),images=images)
        if item['id']!='task-cpu':record['gpu_device_ids']=[A if item['id']=='task-one' else B]
        content['tasks'][item['id']]=record
    resources.write_text(json.dumps(task_resources.seal(content)), encoding='utf-8')
    cfg=original['config'];cfg.update(tasks=['task-one','task-cpu','task-disjoint'],repeats=2,workers=3)
    plan=eval_plan.compile_plan(cfg);directory=evaluation.prepare(plan,tmp_path/'gpu-scheduler')
    catalog={job['id']:job for job in plan['jobs']};started=[];children=[]
    release=tmp_path/'release-shared-gpu'
    def launch(folder,identifier,attempt):
        selected=catalog[identifier];ids=set(selected['resources'].get('gpu_device_ids',[]))
        with evaluation.database(folder) as connection:
            for row in connection.execute("SELECT id,spec FROM jobs WHERE status='running'"):
                if row['id']==identifier:continue
                occupied=set(json.loads(row['spec'])['resources'].get('gpu_device_ids',[]))
                assert not ids & occupied,'scheduler dispatched two unfinished jobs on the same GPU'
        started.append(identifier)
        # Hold the first GPU job until independent work is dispatched. A fixed
        # sleep can expire while NTFS input copies or a loaded host are slow.
        if identifier==plan['jobs'][4]['id']:
            release.touch()
        process=fake_launch(folder,identifier,attempt,delay=0.03,
            release=release if identifier==plan['jobs'][0]['id'] else None)
        children.append(process);return process
    try:
        result=evaluation.schedule(directory,launch=launch,interval=0.01)
        assert result['completed']==6
        assert started[:3]==[plan['jobs'][index]['id'] for index in (0,2,3)]
        assert started.index(plan['jobs'][4]['id']) < started.index(plan['jobs'][1]['id'])
    finally:
        for process in children:
            if process.poll() is None:process.kill()
            _,errors=process.communicate(timeout=5)
            assert process.returncode==0,errors.decode()
