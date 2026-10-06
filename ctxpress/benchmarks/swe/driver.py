"""Frozen local SWE-bench dispatch and recovery; no implicit preparation."""
from __future__ import annotations
import json, os, re, signal, subprocess, time, urllib.parse, uuid
from pathlib import Path
from ctxpress.harness.jobs import environment as eval_environment, plan as eval_plan, resources as task_resources
from ctxpress.core import processes
from ctxpress.live.telemetry import summary
from ctxpress.harness.runtime import codex_agent
from ctxpress.harness.runtime import docker as runtime_docker, method_inputs, socket_bridge
from ctxpress.benchmarks.swe import protocol as swe_protocol

WORKER=Path(__file__).with_name('worker.py')


def prepare(task,entry,config,job,folder,label):
    environment=config['environment']
    catalog=codex_agent.catalog_input(environment,config['model'],config['reasoning'])
    lock,_=task_resources.read(environment['resources'],task['benchmark'],[job['task']])
    if job.get('resources')!=lock['tasks'][task['id']]:raise ValueError('SWE-bench job resource binding changed')
    if task['benchmark']=='bigcodebench':
        from ctxpress.benchmarks.bigcode import protocol
        mode='codebench'
    elif task['benchmark']=='swe-bench-pro' and task['initial_state'].get('pro_version')=='v1':
        from ctxpress.benchmarks.pro import v1 as protocol
        mode='pro-v1'
    elif task['benchmark']=='swe-polybench':
        from ctxpress.benchmarks.polybench import protocol
        mode='polybench'
    else:protocol=swe_protocol;mode=swe_protocol.api(lock)
    missing=protocol.requirements(config,[task],lock)
    if missing:raise ValueError('; '.join(missing))
    protocol.instance(task)
    official=Path(environment['official_root']).resolve()
    for key,tree in lock['trees'].items():
        actual=eval_environment.workspace(official/key)
        if not eval_environment.same_tree(actual, tree):
            raise ValueError('frozen SWE-bench official tree changed: '+key)
    runtime=lock['runtime']
    if eval_plan.file_sha256(runtime['python'])!=runtime['sha256']:raise ValueError('pinned SWE-bench Python changed')
    upstream=environment.get('upstream') or 'https://chatgpt.com/backend-api/codex'
    url=urllib.parse.urlsplit(upstream)
    if url.scheme!='https' or not url.hostname or url.username is not None or url.password is not None or url.fragment:
        raise ValueError('SWE-bench upstream requires HTTPS without embedded credentials')
    target=('['+url.hostname+']' if ':' in url.hostname else url.hostname)+':'+str(url.port or 443)
    if environment.get('via'):
        from ctxpress.harness.runtime.connect_proxy import proxy_address
        proxy_address(environment['via'])
    binary=Path(environment['bindir'])/'codex'
    from ctxpress.harness.runtime import codex_binary
    readiness=codex_binary.preflight(binary.parent)
    version='codex-cli '+(readiness['version'] or codex_binary.version(binary))
    match=re.fullmatch(r'codex-cli\s+(\S+)',version)
    if not match:raise ValueError('invalid pinned Codex version')
    model=config['model'].replace('/','__')
    if model in ('.','..') or not re.fullmatch(r'[A-Za-z0-9_.:-]+',model):raise ValueError('model identity cannot escape official report paths')
    auth=os.environ.get('CTXPRESS_CODEX_AUTH_FILE')
    if not auth or not Path(auth).is_file():raise ValueError('set existing runtime CTXPRESS_CODEX_AUTH_FILE credentials')
    folder=Path(folder).resolve();folder.mkdir(parents=True,exist_ok=True)
    profiles=folder/'method-inputs';resource=job['resources']
    request=dict(schema='ctxpress.eval.swe_trial',version=1,project='ctxp-sw-'+uuid.uuid4().hex[:24],label=label,
        task=task,method=method_inputs.freeze(entry,profiles),model=config['model'],reasoning=config['reasoning'],
        run=config['run'],compact_limit=job['compact_limit'],binary_version=match.group(1),bindir=str(binary.parent),
        package=str(Path(__file__).resolve().parents[3]),official=str(official),profiles=str(profiles),folder=str(folder),runtime=runtime,
        api=mode,agent_image=resource['images']['agent']['id'],grading_image=resource['images']['grading']['verifier']['id'],
        verifier_cap_add=resource.get('verifier_cap_add',[]),upstream=upstream,target=target,via=environment.get('via'),**catalog)
    if mode=='codebench':request.update(sample_index=job['repeat'],n_samples=config['repeats'])
    return request,str(Path(auth).resolve())


def recover(path,label):
    from ctxpress.benchmarks.swe.containers import SCHEMA
    path=Path(path);record=json.loads(path.read_text(encoding='utf-8'))
    if (record.get('schema')!=SCHEMA or record.get('version')!=1 or record.get('label')!=label or
            not re.fullmatch(r'ctxp-sw-[0-9a-f]{24}',record.get('project','')) or record.get('role') not in ('agent','verifier') or
            record.get('container')!=record['project']+('-agent' if record['role']=='agent' else '-grade') or
            not re.fullmatch(r'sha256:[0-9a-f]{64}',record.get('image',''))):
        raise ValueError('invalid SWE-bench recovery owner')
    if record.get('cleaned'):
        if record['role']=='agent':socket_bridge.cleanup_channel(record)
        return
    if type(record.get('pid')) is int and processes.alive(record['pid'],record.get('identity')):
        raise ValueError('SWE-bench worker is still alive; recovery cannot interrupt it')
    docker=runtime_docker.docker
    if docker('info','--format','{{.ID}}').strip()!=record.get('daemon_id'):
        raise ValueError('SWE-bench recovery Docker daemon changed')
    ids=docker('ps','-aq','--filter','name=^/'+record['container']+'$').split()
    if len(ids)>1:raise ValueError('ambiguous SWE-bench owned container')
    for identifier in ids:
        value=json.loads(docker('container','inspect',identifier))[0];labels=value.get('Config',{}).get('Labels') or {}
        if (value.get('Image')!=record['image'] or labels.get('ctxpress.managed')!='true' or labels.get('ctxpress.run')!=label or
                labels.get('ctxpress.swe.role')!=record['role'] or value.get('Name','').lstrip('/')!=record['container'] or
                record.get('container_id') and value.get('Id')!=record['container_id']):
            raise ValueError('SWE-bench recovery container ownership changed')
    for identifier in ids:
        if record.get('credentials_may_exist'):
            docker('stop','-t','5',identifier);docker('start',identifier)
            docker('exec','--user','root',identifier,'sh','-c',
                'rm -f /ctxpress-private/codex/auth.json; test ! -e /ctxpress-private/codex/auth.json && test ! -L /ctxpress-private/codex/auth.json')
            docker('stop','-t','5',identifier)
        docker('rm','-f','-v',identifier)
    record.update(cleaned=True,phase='recovered',credentials_may_exist=False);eval_plan.atomic_json(path,record)
    if record['role']=='agent':socket_bridge.cleanup_channel(record)


def execute(adapter,task,entry,config,job,*,paths,folder,label):
    folder=Path(folder).resolve();folder.mkdir(parents=True,exist_ok=True)
    request,auth=prepare(task,entry,config,job,folder,label)
    request_path=folder/'swe-request.json';eval_plan.atomic_json(request_path,request)
    command=[request['runtime']['python'],'-I','-S','-B',str(WORKER),str(request_path)]
    env={key:os.environ[key] for key in ('PATH','DOCKER_HOST','DOCKER_CONTEXT','DOCKER_CONFIG','XDG_RUNTIME_DIR','HOME','TEMP','TMP') if key in os.environ}
    env['CTXPRESS_CODEX_AUTH_FILE']=auth
    started=time.monotonic()
    with (folder/'swe-worker.log').open('wb') as output:
        subprocess.run(command+['--check'],env=env,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,check=True,timeout=60)
        process=subprocess.Popen(command,env=env,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        try:code=process.wait()
        except BaseException:
            os.killpg(process.pid,signal.SIGTERM)
            try:process.wait(timeout=45)
            except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
            raise
        finally:
            for journal in folder.glob('resources-swe-*.json'):recover(journal,label)
    if code:raise RuntimeError('official SWE-bench worker failed; see local worker log')
    state=json.loads((folder/'swe-worker-result.json').read_text(encoding='utf-8'))
    logs=folder/'agent'/'ctxpress-requests.jsonl';rows=[]
    if logs.is_file():
        for line in logs.read_text(encoding='utf-8').splitlines():
            try:row=json.loads(line)
            except ValueError:continue
            if isinstance(row,dict):rows.append(row)
    telemetry=summary(str(logs))
    report=state.get('official_report')
    grade=adapter.read_grade(task,report or folder/'missing-report.json') if config['run']['grade'] else None
    if grade is not None and state.get('agent_exception'):grade['agent_exception']={'exception_type':state['agent_exception']}
    result=dict(task=task,benchmark=adapter.describe(),method=entry,model=config['model'],reasoning=config['reasoning'],
        stop=state['stop'],calls=state['calls'],seconds=round(time.monotonic()-started,1),requests=telemetry['requests'],
        usage=telemetry,rewrites=rows,grade=grade,proxy_log=str(logs),binary_version=request['binary_version'],
        **{field:state.get(field) for field in ('official_report','official_artifacts','separate_verifier','grading_run_id','swe_api','protocol','network_policy',
                                              'pro_version','regrade_report','submission','fresh_regrade',
                                              'code_samples','sample_id','artifact_kind') if field in state},
        real_run_verified=False)
    if catalog:=codex_agent.check_catalog(request):result['model_catalog']=catalog
    from ctxpress.harness.runtime import execution_health
    return execution_health.retain(result)
