"""Frozen native itinerary preparation, continuous dispatch and recovery."""
from __future__ import annotations
import copy, json, os, re, signal, subprocess, time, uuid
from pathlib import Path
from ctxpress.harness import eval_environment, eval_plan, task_resources, task as task_api
from . import milestone_protocol
from ctxpress.live.telemetry import summary

WORKER=Path(__file__).resolve().parents[1]/'harness'/'milestone_worker.py'
IMAGE_CHECK=Path(__file__).with_name('milestone_images.py')


def request(task,config,job,folder):
    task_api.verify_remap(job['task'],task)
    environment=config['environment'];official=Path(environment['official_root']).resolve()
    lock,_=task_resources.read(environment['resources'],'swe-milestone',[job['task']])
    if task['benchmark']!='swe-milestone' or job.get('resources')!=lock['tasks'][task['id']]:
        raise ValueError('native itinerary job/resource identity changed')
    selected=copy.deepcopy(lock)
    for key,tree in selected['trees'].items():
        tree['root']=str(official/key);actual=eval_environment.workspace(tree['root'])
        if not eval_environment.same_tree(actual,tree):
            raise ValueError('native official input tree changed: '+key)
    missing=milestone_protocol.requirements(config,[task],selected)
    if missing:raise ValueError('; '.join(missing))
    return dict(schema='ctxpress.eval.milestone_preflight',version=1,task=task,original_task=job['task'],
        resources=environment['resources'],official=str(official),folder=str(Path(folder).resolve()),
        package=str(Path(__file__).resolve().parents[2]))


def _invoke(task,config,job,folder,mode,trial=None):
    folder=Path(folder).resolve();folder.mkdir(parents=True,exist_ok=True)
    payload=request(task,config,job,folder);path=folder/('milestone-'+mode+'-request.json')
    if trial is not None:payload['trial']=str(Path(trial).absolute())
    eval_plan.atomic_json(path,payload)
    lock,_=task_resources.read(payload['resources'],'swe-milestone',[job['task']])
    command=[lock['runtime']['python'],'-I','-S','-B',str(WORKER),str(path),'--'+mode]
    # Preparation has no Docker/credential environment, package installation or
    # model endpoint. Actual dispatch will supply its separately owned channel.
    with (folder/('milestone-'+mode+'.log')).open('wb') as output:
        subprocess.run(command,env={},stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,check=True,timeout=60)
    return folder


def preflight(task,config,job,folder):
    """No authentication or model transport is opened by this preparation check."""
    folder=_invoke(task,config,job,folder,'check')
    return json.loads((folder/'native-preparation'/'preparation.json').read_text(encoding='utf-8'))


def check_images(payload):
    """Inspect prepared images through the author API before credentials or Agent work."""
    folder=Path(payload['folder']);folder.mkdir(parents=True,exist_ok=True)
    path=folder/'milestone-images-request.json';eval_plan.atomic_json(path,payload)
    lock,_=task_resources.read(payload['resources'],'swe-milestone',[payload['original_task']])
    command=[lock['runtime']['python'],'-I','-S','-B',str(IMAGE_CHECK),str(path)]
    environment={key:os.environ[key] for key in ('PATH','DOCKER_HOST','DOCKER_CONTEXT','DOCKER_CONFIG',
        'XDG_RUNTIME_DIR','SYSTEMROOT','WINDIR','TEMP','TMP','HOME') if key in os.environ}
    with (folder/'milestone-images.log').open('wb') as output:
        subprocess.run(command,env=environment,stdin=subprocess.DEVNULL,stdout=output,
            stderr=subprocess.STDOUT,check=True,timeout=60)
    return json.loads((folder/'native-images.json').read_text(encoding='utf-8'))


def read_grade(task,config,job,folder,trial):
    """Collect existing native results; this does not invoke a grader or Agent."""
    folder=_invoke(task,config,job,folder,'read-grade',trial)
    return json.loads((folder/'native-grade.json').read_text(encoding='utf-8'))


def prepare_execution(task,entry,config,job,folder,label):
    from ctxpress.harness import milestone_resources, milestone_transport, milestone_version
    from ctxpress.methods import build
    from .harbor_driver import method_inputs
    payload=request(task,config,job,folder)
    lock,_=task_resources.read(payload['resources'],'swe-milestone',[job['task']])
    if 'native_data_version' not in lock:
        raise ValueError('capture native_data_version=true before continuous native execution')
    milestone_version.validate(lock['native_data_version'])
    if config['run'].get('grade') is not True:
        raise ValueError('native itinerary execution requires its dependency grading')
    build(entry).validate_live()
    environment=config.get('environment',{})
    catalog_binding={}
    if 'model_catalog' in environment:
        from .milestone_codex import catalog_input
        catalog_binding=catalog_input(environment,config['model'],config['reasoning'])
    check_images(payload)
    environment=config['environment'];upstream=environment.get('upstream') or 'https://chatgpt.com/backend-api/codex'
    milestone_transport.destination(upstream)
    if environment.get('via'):
        from ctxpress.harness.connect_proxy import proxy_address
        proxy_address(environment['via'])
    project='ctxp-ms-'+uuid.uuid4().hex[:24];milestone_resources.identity(project,label)
    binary=Path(environment['bindir']).resolve()/'codex'
    from ctxpress.harness import codex_binary
    readiness=codex_binary.preflight(binary.parent)
    version='codex-cli '+(readiness['version'] or codex_binary.version(binary))
    match=re.fullmatch(r'codex-cli\s+(\S+)',version)
    if not match:raise ValueError('pinned Codex binary returned an invalid version')
    auth=os.environ.get('CTXPRESS_CODEX_AUTH_FILE')
    if not auth or Path(auth).is_symlink() or not Path(auth).is_file():
        raise ValueError('set CTXPRESS_CODEX_AUTH_FILE to existing credentials; auth is never frozen in the plan')
    profiles=Path(folder).resolve()/'method-inputs'
    payload['execution']=dict(project=project,label=label,method=method_inputs(entry,profiles),
        model=config['model'],reasoning=config['reasoning'],run=copy.deepcopy(config['run']),
        compact_limit=job['compact_limit'],binary_version=match.group(1),bindir=str(binary.parent),
        profiles=str(profiles),upstream=upstream,via=environment.get('via'))
    payload['execution'].update(catalog_binding)
    return payload,lock['runtime'],str(Path(auth).resolve())


def recover(path,label):
    from ctxpress.harness import milestone_resources, milestone_transport
    if Path(path).name.startswith('resources-milestone-services-'):
        from ctxpress.harness import service_gateway
        return service_gateway.recover(path,label)
    if Path(path).name=='resources-milestone-channel.json':return milestone_transport.recover(path,label)
    return milestone_resources.recover(path,label)


def recover_attempt(folder,label):
    # Remove credential-bearing Agent first, then verifiers, network and relay.
    journals=list(Path(folder).glob('resources-milestone-*.json'))
    def order(path):
        if path.name=='resources-milestone-channel.json':return 4
        if path.name=='resources-milestone-network.json':return 3
        if path.name.startswith('resources-milestone-services-'):return 2
        return 0 if path.name.endswith('-agent.json') else 1
    for path in sorted(journals,key=lambda value:(order(value),value.name)):recover(path,label)


def execute(adapter,task,entry,config,job,*,paths,folder,label):
    folder=Path(folder).resolve();folder.mkdir(parents=True,exist_ok=True)
    payload,runtime,auth=prepare_execution(task,entry,config,job,folder,label)
    path=folder/'milestone-execute-request.json';eval_plan.atomic_json(path,payload)
    command=[runtime['python'],'-I','-S','-B',str(WORKER),str(path)]
    child_env={key:os.environ[key] for key in ('PATH','DOCKER_HOST','DOCKER_CONTEXT','DOCKER_CONFIG',
        'XDG_RUNTIME_DIR','SYSTEMROOT','WINDIR','TEMP','TMP','HOME') if key in os.environ}
    child_env['CTXPRESS_CODEX_AUTH_FILE']=auth
    started=time.monotonic()
    with (folder/'milestone-execute.log').open('wb') as output:
        # Import/contract checks must precede Docker and credentials. This mode
        # deliberately leaves trial preparation to the execution worker.
        subprocess.run(command+['--validate-execution'],env=child_env,stdin=subprocess.DEVNULL,
            stdout=output,stderr=subprocess.STDOUT,check=True,timeout=60)
        process=subprocess.Popen(command+['--execute'],env=child_env,stdin=subprocess.DEVNULL,
            stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        try:code=process.wait()
        except BaseException:
            if process.poll() is None:
                try:os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError:pass
            try:process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.wait()
            raise
        finally:recover_attempt(folder,label)
    if code:raise RuntimeError('official native milestone worker failed; see milestone-execute.log')
    state=json.loads((folder/'native-execution.json').read_text(encoding='utf-8'))
    grade=json.loads((folder/'native-grade.json').read_text(encoding='utf-8'))
    if state.get('cleanup_complete') is not True:raise RuntimeError('native worker cleanup is incomplete')
    logs=folder/'requests.jsonl';rows=[]
    for source in sorted((folder/'agent-logs').glob('*.jsonl')):
        for line in source.read_text(encoding='utf-8').splitlines():
            try:row=json.loads(line)
            except ValueError:continue
            if isinstance(row,dict):rows.append(row)
    rows.sort(key=lambda row:row.get('t',0))
    with logs.open('w',encoding='utf-8') as stream:
        for row in rows:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
    telemetry=summary(str(logs))
    result=dict(task=task,task_id=task['id'],start_mode='task_start',benchmark=adapter.task_start_description(),
        method=entry,model=config['model'],reasoning=config['reasoning'],stop=state['stop'],
        seconds=round(time.monotonic()-started,1),calls=state['calls'],requests=telemetry['requests'],
        usage=telemetry,rewrites=rows,grade=grade,proxy_log=str(logs),official_report=str(folder/'native-grade.json'),
        binary_version=payload['execution']['binary_version'],protocol='ctxpress_comparison',real_run_verified=False)
    if 'model_catalog' in state:result['model_catalog']=state['model_catalog']
    from ctxpress.harness import execution_health
    return execution_health.retain(result)
