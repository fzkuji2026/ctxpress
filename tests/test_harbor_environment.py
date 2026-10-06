"""Compose/lifecycle contracts with no daemon, download, task or model execution."""
import asyncio, copy, json
from pathlib import Path
from types import SimpleNamespace
import pytest
from ctxpress.benchmarks.harbor_environment import framework, guarded_compose
from ctxpress.harness import eval_environment


IMAGE = 'sha256:' + 'a'*64
SERVICE = 'sha256:' + 'b'*64


def model(tmp_path):
    return dict(name='ambient-project', services={
        'main':dict(image='moving:main', build={'context':'.'}, labels={'task':'fixture'},
            volumes=[dict(type='bind',source=str(tmp_path/'runtime'),target='/ctxpress-runtime')],
            networks={'default':None}, ports=['8080:8080'],
            deploy={'resources':{'limits':{'cpus':'2','memory':'8G'}}}),
        'db':dict(image='moving:db',networks={'default':{'aliases':['database']}},
            volumes=[dict(type='volume',source='dbdata',target='/var/lib/db')])},
        networks={'default':{'name':'ambient-network'}}, volumes={'dbdata':{'name':'ambient-data'}})


def settings(tmp_path):
    return dict(images={'main':IMAGE,'db':SERVICE},bind_roots={str(tmp_path/'runtime'):True},run_label='plan-job')


def test_multi_service_resources_are_preserved_with_private_owned_networks(tmp_path):
    original=model(tmp_path); before=copy.deepcopy(original)
    guarded=guarded_compose(original,**settings(tmp_path))
    assert original == before
    assert guarded['services']['main']['deploy'] == original['services']['main']['deploy']
    assert guarded['services']['db']['networks']['default']['aliases'] == ['database']
    assert guarded['services']['main']['image'] == IMAGE and guarded['services']['db']['image'] == SERVICE
    assert guarded['networks']['default']['internal'] and not guarded['networks']['default']['external']
    assert guarded['networks']['default']['labels']['ctxpress.run'] == 'plan-job'
    assert guarded['volumes']['dbdata']['labels']['ctxpress.run'] == 'plan-job'
    assert guarded['services']['main']['volumes'][0]['read_only']
    assert guarded['services']['main']['volumes'][0]['bind']['create_host_path'] is False
    assert all(service['labels']['ctxpress.run']=='plan-job' for service in guarded['services'].values())
    assert 'build' not in guarded['services']['main'] and 'ports' not in guarded['services']['main']
    assert 'name' not in guarded


@pytest.mark.parametrize('change',['missing-image','moving-image','host-network','host-bind','auth-bind',
    'docker-socket','external-volume','private-bind','private-alias','volume-driver','host-secret','host-privileges'])
def test_undeclared_resources_fail_before_a_container_can_be_started(tmp_path,change):
    value=model(tmp_path); cfg=settings(tmp_path); main=value['services']['main']; bind=main['volumes'][0]
    if change=='missing-image':cfg['images'].pop('db')
    if change=='moving-image':cfg['images']['main']='latest'
    if change=='host-network':main['network_mode']='host'
    if change=='host-bind':bind['source']=str(tmp_path/'outside')
    if change=='auth-bind':bind['source']=str(tmp_path/'runtime/auth.json')
    if change=='docker-socket':bind['source']=str(tmp_path/'runtime/docker.sock')
    if change=='external-volume':value['volumes']['dbdata']['external']=True
    if change=='private-bind':bind['target']='/ctxpress-private/codex'
    if change=='private-alias':bind['target']='/logs/../ctxpress-private/codex'
    if change=='volume-driver':value['volumes']['dbdata']['driver_opts']={'device':'/host','type':'none','o':'bind'}
    if change=='host-secret':value['secrets']={'auth':{'file':str(tmp_path/'auth.json')}}
    if change=='host-privileges':main['privileged']=True
    with pytest.raises(ValueError):guarded_compose(value,**cfg)


def test_network_none_is_preserved_and_service_namespaces_stay_internal(tmp_path):
    value=model(tmp_path);value['services']['main']['network_mode']='none';value['services']['main'].pop('networks')
    assert guarded_compose(value,**settings(tmp_path))['services']['main']['network_mode']=='none'
    value=model(tmp_path);value['services']['main']['network_mode']='service:db';value['services']['main'].pop('networks')
    assert guarded_compose(value,**settings(tmp_path))['services']['main']['network_mode']=='service:db'


class OfficialFixture:
    def __init__(self, folder, **kwargs):
        self.session_id='ctxp-hb-'+'a'*24
        self.environment_dir=folder/'environment';self.environment_dir.mkdir()
        self.trial_paths=SimpleNamespace(trial_dir=folder)
        self.task_env_config=SimpleNamespace(docker_image='moving')
        self._env_vars=SimpleNamespace(prebuilt_image_name='moving',
            to_env_dict=lambda include_os_env:{'DECLARED':'value'})
        self._mounts_json=None
        self.calls=[]
    @property
    def _docker_compose_paths(self):return [self.environment_dir/'official-compose.yaml']
    async def _chown_to_host_user(self,*args,**kwargs):self.calls.append(['chown'])


def test_official_omitted_gpu_config_can_initialize_a_cpu_environment(tmp_path):
    class CPUFixture(OfficialFixture):
        def __init__(self, folder, **kwargs):
            super().__init__(folder, **kwargs)
            self.task_env_config.gpus = None
            self.task_env_config.gpu_types = None
    async def cleanup(environment):pass
    instance=framework(CPUFixture,SimpleNamespace,settings(tmp_path),cleanup)(tmp_path)
    assert instance._ctxpress_gpu_requirements == {'count':0,'types':None}
    # Only the typed model's omitted value is normalized; raw declarations
    # still reject invalid GPU requests before launching an environment.
    from ctxpress.benchmarks.harbor_gpu import requirements
    with pytest.raises(ValueError,match='nonnegative integer'):
        requirements({'gpus':None})


def test_stop_checks_credential_cleanup_before_down_and_never_removes_images(tmp_path):
    order=[]
    async def cleanup(environment):order.append('credentials')
    instance=framework(OfficialFixture,SimpleNamespace,settings(tmp_path),cleanup)(tmp_path)
    async def command(args,**kwargs):order.append(args);return SimpleNamespace(return_code=0)
    instance._run_docker_compose_command=command;instance._ctxpress_started=True
    asyncio.run(instance.stop())
    assert order==['credentials',['down','--volumes','--remove-orphans']]
    assert not instance._ctxpress_started


def test_stop_preserves_container_when_credential_cleanup_fails(tmp_path):
    async def cleanup(environment):raise RuntimeError('preserve managed container')
    instance=framework(OfficialFixture,SimpleNamespace,settings(tmp_path),cleanup)(tmp_path)
    async def command(args,**kwargs):pytest.fail('deleted environment after failed credential cleanup')
    instance._run_docker_compose_command=command;instance._ctxpress_started=True
    with pytest.raises(RuntimeError,match='preserve'):asyncio.run(instance.stop())
    assert instance._ctxpress_started


def test_start_guards_all_services_before_official_up_without_stale_down(tmp_path,monkeypatch):
    async def cleanup(environment):pass
    instance=framework(OfficialFixture,SimpleNamespace,settings(tmp_path),cleanup)(tmp_path)
    async def command(args,**kwargs):
        instance.calls.append(args)
        return SimpleNamespace(return_code=0,stdout=json.dumps(model(tmp_path)))
    instance._run_docker_compose_command=command
    monkeypatch.setattr(eval_environment,'image',lambda reference:{'id':reference})
    asyncio.run(instance.start())
    assert instance.calls==[['config','--format','json'],['up','--detach','--wait']]
    declared=json.loads(instance._ctxpress_guard_path.read_text(encoding='utf-8'))
    assert declared['services']['db']['image']==SERVICE and declared['networks']['default']['internal']
    assert instance._docker_compose_paths==[instance._ctxpress_guard_path]


def test_compose_exec_refuses_installation_and_excludes_ambient_credentials(tmp_path,monkeypatch):
    async def cleanup(environment):pass
    instance=framework(OfficialFixture,SimpleNamespace,settings(tmp_path),cleanup)(tmp_path)
    monkeypatch.setenv('OPENAI_API_KEY','fixture-not-a-real-secret')
    captured=[]
    class Process:
        returncode=0
        async def communicate(self):return b'{}',b''
    async def launch(*args,**kwargs):captured.append((args,kwargs));return Process()
    monkeypatch.setattr(asyncio,'create_subprocess_exec',launch)
    for args in (['pull'],['build'],['down','--rmi','all']):
        with pytest.raises(ValueError):asyncio.run(instance._run_docker_compose_command(args))
    assert not captured
    asyncio.run(instance._run_docker_compose_command(['up','--detach','--wait']))
    args,kw=captured[0]
    assert args[-6:]==('up','--pull','never','--no-build','--detach','--wait')
    assert 'OPENAI_API_KEY' not in kw['env'] and kw['env']['DECLARED']=='value'
