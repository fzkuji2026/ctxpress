"""Compose the frozen native trial with synthetic Docker/model/test IO.

This check uses real local Git commits for submission archives and the author's
Trial, watcher, DAG, Agent commands, result writer and collector. Container
runtime checks, model output and test execution remain explicit substitutes.
It does not prove a real task is runnable and never contacts a Docker daemon.
"""
import argparse, contextlib, copy, io, json, os, shlex, subprocess, sys, tarfile, threading, time
from pathlib import Path
from unittest.mock import patch


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--case',choices=['complete','timeout'],default='complete')
    args=parser.parse_args()
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    from milestone_mock_engine import Daemon,serving
    from ctxpress.harness.milestone_worker import load
    from ctxpress.harness import eval_plan, milestone_agent, milestone_containers, milestone_images
    from ctxpress.harness import milestone_resources, milestone_runner, milestone_transport, task_resources,service_gateway
    request=json.loads(args.request.read_text(encoding='utf-8'));source=load(request)
    from harness.e2e import agent_runner, container_setup, evaluator, orchestrator, run_e2e
    lock,_=task_resources.read(request['resources'],'swe-milestone',[request['original_task']])
    image=lock['tasks'][request['task']['id']]['images']['agent']['id']
    root=Path(request['folder']).parent/('lifecycle-'+args.case+'-check');root.mkdir()
    request=copy.deepcopy(request);request['folder']=str(root/'attempt');Path(request['folder']).mkdir()
    bindir=root/'bin';bindir.mkdir();profiles=root/'profiles';profiles.mkdir()
    request['execution']=dict(project='ctxp-ms-'+'2'*24,label='synthetic-native-lifecycle',
        method={'class':'NoCompaction'},model='synthetic-model',reasoning='low',run=dict(timeout=45 if args.case=='complete' else .1,max_calls=10,grade=True,grading_timeout=10),
        compact_limit=230000,binary_version='0.159.0-alpha.12.1',bindir=str(bindir),profiles=str(profiles),
        upstream='https://fixture.invalid/responses',via=None)
    auth=root/'synthetic-auth.json';auth.write_text('{}', encoding='utf-8')
    repo=root/'synthetic-testbed';repo.mkdir();(repo/'src').mkdir();(repo/'src/app.py').write_text('value = 0\n', encoding='utf-8')
    real_run=subprocess.run
    def git(*command,**options):
        return real_run(['git','-C',str(repo),'-c','user.name=fixture','-c','user.email=fixture@example.invalid',*command],**options)
    git('init','-q');git('add','.');git('commit','-qm','synthetic task start')
    events=[];native=[];invocations=[];evaluated=[];guard=threading.RLock();engine=Daemon()
    engine.image_config={'Volumes':{'/data':{}}};service_exercised=[]
    class Registry(milestone_resources.Registry):
        def release_trial(self,record):
            super().release_trial(record)
            events.append('drained')
        def cleanup(self):
            super().cleanup()
            events.append('registry-cleaned')

    class Channel(milestone_transport.Channel):
        def cleanup(self):super().cleanup();events.append('channel-cleaned')

    original_stop=milestone_resources.Owner.stop_agent
    def stop_owner(owner):
        original_stop(owner)
        if owner.record['role']=='agent':events.append('quiesce-agent')

    def initialize(owner,framework,auth_path):
        assert Path(auth_path)==auth
        assert owner.record['private_initialized'];framework.set_model_relay('http://127.0.0.1:31234')
        owner.credentials(auth)

    def synthetic_invocation(command):
        values=shlex.split(command)
        assert 'ctxpress.harness.agent_process' in values
        assert values[values.index('--via')+1]=='http://127.0.0.1:31234'
        with guard:
            current=native[0];runnable=current.dag.get_next_runnable();assert len(runnable)==1,runnable
            mid=runnable[0];assert mid not in invocations,invocations
            invocations.append(mid);(repo/'src/app.py').write_text('value = '+mid[1:]+'\n', encoding='utf-8')
            git('add','.');git('commit','-qm','synthetic '+mid);git('tag','agent-impl-'+mid)
        # Allow the native watcher to observe this tag before the synthetic
        # Agent exits; this prevents a polling race from adding fake turns.
        deadline=time.monotonic()+10
        while mid not in current.dag.submitted_milestones and mid not in current.dag.completed_milestones:
            assert time.monotonic()<deadline,'native watcher did not submit '+mid
            time.sleep(.02)
        return json.dumps({'type':'thread.started','thread_id':'synthetic-thread'})+'\n'

    def docker_command(command,**options):
        if command[0]=='git':return real_run(command,**options)
        assert command[:1]==['docker'],command
        text=options.get('text',False);stdout='';code=0
        def api(method,path,value=None):
            result=engine.request(method,path,b'' if value is None else json.dumps(value).encode())
            assert result.status<400,(method,path,result.body)
            return result.value() if result.body else None
        if command[1]=='exec' and 'git' in command:
            index=command.index('git');return git(*command[index+1:],**options)
        if command[1]=='exec' and 'ctxpress.harness.agent_process' in command[-1] and '--stop' not in command[-1]:
            stdout=synthetic_invocation(command[-1])
        elif command[1]=='ps':
            name=next(value.split('name=',1)[1].strip('^/$') for value in command if value.startswith('name='))
            row=engine.objects['containers'].get(name)
            stdout='' if row is None else name if '{{.Names}}' in command else row['Id']
        elif command[1]=='inspect':stdout='true'
        elif command[1]=='info':stdout=engine.daemon
        elif command[1:3]==['image','inspect']:stdout=json.dumps([api('GET','/images/'+command[3]+'/json')])
        elif command[1:3]==['container','inspect']:stdout=json.dumps([api('GET','/containers/'+command[3]+'/json')])
        elif command[1]=='create':
            name=command[command.index('--name')+1]
            labels=dict(command[index+1].split('=',1) for index,arg in enumerate(command) if arg=='--label')
            host=dict(NetworkMode=command[command.index('--network')+1])
            payload=dict(Image=image,Labels=labels,HostConfig=host)
            stdout=api('POST','/containers/create?name='+name,payload)['Id']
        elif command[1]=='start':api('POST','/containers/'+command[2]+'/start')
        elif command[1]=='rm':api('DELETE','/containers/'+command[-1])
        elif command[1:3]==['network','ls']:
            name=command[-1].split('name=',1)[1].strip('^$');row=engine.objects['networks'].get(name)
            stdout='' if row is None else row['Id']
        elif command[1:3]==['network','create']:
            labels=dict(command[index+1].split('=',1) for index,arg in enumerate(command) if arg=='--label')
            stdout=api('POST','/networks/create',dict(Name=command[-1],Labels=labels,Driver='bridge',Internal=True))['Id']
        elif command[1:3]==['network','inspect']:stdout=json.dumps([api('GET','/networks/'+command[3])])
        elif command[1:3]==['network','rm']:api('DELETE','/networks/'+command[3])
        elif command[1]=='cp':pass
        elif command[1]=='exec':
            # Initializer/runtime, SRS/queue permissions, trace copy and stop
            # are synthetic IO; all host process launches remain intercepted.
            pass
        else:raise AssertionError('unexpected synthetic Docker operation: '+repr(command))
        return subprocess.CompletedProcess(command,code,stdout if text else stdout.encode(),'' if text else b'')

    def image_inspect(*command):
        assert command[:2]==('image','inspect'),command
        return json.dumps([dict(Id=command[2],Os='linux',Config={'Labels':{}})])

    def synthetic_tests(check):
        check.start_container()
        try:
            with tarfile.open(check.patch_file) as archive:
                assert 'src/app.py' in archive.getnames()
                assert archive.extractfile('src/app.py').read()==('value = '+check.milestone_id[1:]+'\n').encode()
            mid=check.milestone_id;passed=mid!='M3';evaluated.append(mid)
            if check.ctxpress_services is not None:
                channel=Path(check.ctxpress_services.record['channel'])/'docker.sock'
                def sdk(method,path,body=b''):
                    connection=service_gateway.Connection(channel)
                    try:
                        connection.request(method,path,body,{'Content-Type':'application/json'})
                        response=connection.getresponse();content=response.read()
                        assert response.status<400,(response.status,content)
                        return json.loads(content) if content else None
                    finally:connection.close()
                reference=next(iter(lock['tasks'][request['task']['id']]['images']['services'].values()))['reference']
                identifier=sdk('POST','/containers/create',json.dumps(dict(Image=reference,
                    ExposedPorts={'8008/tcp':{}},HostConfig={'PortBindings':{'8008/tcp':[{'HostPort':'48008'}]}})).encode())['Id']
                stream=io.BytesIO()
                with tarfile.open(fileobj=stream,mode='w') as archive:
                    content=b'server_name: localhost\n';entry=tarfile.TarInfo('data/homeserver.yaml');entry.size=len(content)
                    archive.addfile(entry,io.BytesIO(content))
                sdk('PUT','/containers/'+identifier+'/archive?path=%2F',stream.getvalue())
                sdk('POST','/containers/'+identifier+'/start')
                service_exercised.append(mid)
            return evaluator.EvaluationResult(milestone_id=mid,patch_is_None=False,patch_exists=True,patch_successfully_applied=True,
                resolved=passed,fail_to_pass_success=['synthetic-f2p'] if passed else [],fail_to_pass_failure=[] if passed else ['synthetic-f2p'],
                pass_to_pass_success_count=1,pass_to_pass_failure=[],pass_to_pass_missing=0,none_to_pass_success=[],none_to_pass_failure=[],
                total_tests=2,passed_tests=2 if passed else 1,failed_tests=0 if passed else 1,error_tests=0,skipped_tests=0,
                fail_to_pass_required=1,fail_to_pass_achieved=int(passed),pass_to_pass_required=1,none_to_pass_required=0,none_to_pass_achieved=0)
        finally:check.cleanup()

    original_orchestrator=orchestrator.E2EOrchestrator.__init__
    def capture(self,*a,**kw):
        original_orchestrator(self,*a,**kw);native.append(self)
        self.config.config['retry_and_timing'].update(debounce_seconds=0,max_debounce_wait=5,evaluation_timeout=10)
        self.config.config['dag_unlock']['early_unblock']=True

    def stream(self,command,path):
        stdout=synthetic_invocation(command[-1]);(self.log_dir/'agent_stdout.txt').write_text(stdout, encoding='utf-8')
        return True

    before=run_e2e.E2ETrialRunner
    with contextlib.ExitStack() as stack:
        endpoint=stack.enter_context(serving(engine,root/'fake.sock'))
        replacements=[(subprocess,'run',docker_command),
            (milestone_resources,'Registry',Registry),(milestone_transport,'Channel',Channel),(milestone_transport,'initialize',initialize),
            (milestone_resources.Owner,'stop_agent',stop_owner),
            (milestone_images,'docker',image_inspect),
            (milestone_agent,'docker',lambda *args:events.append('stop-invocation')),
            (milestone_containers,'docker',lambda *args:''),
            (container_setup,'inspect_docker_image_id',lambda *args,**kw:image[7:]),
            (orchestrator,'inspect_docker_image_id',lambda *args,**kw:image[7:]),
            (container_setup.ContainerSetup,'get_agent_version',lambda *args,**kw:request['execution']['binary_version']),
            (container_setup.ContainerSetup,'truncate_git_history',lambda *args:None),
            (container_setup.ContainerSetup,'_wait_for_fakeroot',lambda *args,**kw:True),
            (container_setup.ContainerSetup,'verify_runtime_environment',lambda *args:None),
            (container_setup.ContainerSetup,'prepare_agent_invocation',lambda *args:None),
            (orchestrator.E2EOrchestrator,'__init__',capture),
            (agent_runner.AgentRunner,'_execute_with_streaming',stream),
            (evaluator.PatchEvaluator,'evaluate',synthetic_tests)]
        for name in ('_install_jest_ipc_guard','_verify_evaluator_go_toolchain','_verify_evaluator_cache_policy'):
            replacements.append((evaluator.PatchEvaluator,name,lambda *args:None))
        for obj,name,value in replacements:stack.enter_context(patch.object(obj,name,value))
        stack.enter_context(patch.dict(os.environ,{'CTXPRESS_CODEX_AUTH_FILE':str(auth),'DOCKER_HOST':'unix://'+str(endpoint)}))
        state=milestone_runner.run(request,source)
    assert run_e2e.E2ETrialRunner is before
    grade=json.loads((Path(request['folder'])/'native-grade.json').read_text(encoding='utf-8'))
    assert state['cleanup_complete'],state
    assert all(not rows for rows in engine.objects.values())
    assert all(json.loads(path.read_text(encoding='utf-8'))['cleaned'] for path in Path(request['folder']).glob('resources-milestone-*.json'))
    assert grade['official_metrics']['graded']==2,grade
    if args.case=='complete':
        assert invocations==['M1','M2','M3'] and sorted(evaluated)==['M1','M2','M3'],(invocations,evaluated)
        assert state['author_success'] and state['stop']=='completed',state
        assert grade['scoring_complete'] and grade['resolved'] is False and grade['official_metrics']['resolved']==1,grade
        assert service_exercised==['M2'] and engine.archives
    else:
        assert set(invocations)<= {'M1'} and set(evaluated)<= {'M1'},(invocations,evaluated)
        assert not state['author_success'] and state['stop']=='timeout',state
        assert not grade['scoring_complete'] and grade['resolved'] is None and grade['official_metrics']['resolved']==0,grade
    trial=Path(state['trial']);drain=json.loads((trial/'ctxpress-drain.json').read_text(encoding='utf-8'))
    assert drain['watcher_joined'] and drain['author_cleanup_returned'] and drain['watcher_exited_clean'],drain
    assert events.index('quiesce-agent')<events.index('drained')<events.index('registry-cleaned')<events.index('channel-cleaned')
    result=dict(test_only=True,case=args.case,native_trial_composition_verified=True,author_trial_and_watcher_used=True,
        author_git_submission_archive_used=any((trial/'evaluation').glob('*/source_snapshot.tar')),
        author_async_result_writer_used=bool(evaluated),author_collector_used=True,
        synthetic_agent_invocations=invocations,synthetic_test_evaluations=sorted(evaluated),
        graded_milestones=2,synthetic_resolved_milestones=grade['official_metrics']['resolved'],drain_verified=True,
        budget_timeout_verified=args.case=='timeout',
        native_resource_provider_used=True,native_model_channel_used=True,native_service_gateway_used=bool(service_exercised),
        substituted=['Docker IO','container runtime checks','model initialization/output','test execution'],
        containers_started=0,model_calls=0,real_run_verified=False)
    eval_plan.atomic_json(args.output,result);print(json.dumps(result))


if __name__=='__main__':main()
