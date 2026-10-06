"""Exercise frozen author overlay checks against synthetic prepared-image metadata."""
import argparse,contextlib,hashlib,io,json,shlex,subprocess,sys
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--request',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path);args=parser.parse_args()
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from ctxpress.harness.milestone_worker import load
    request=json.loads(args.request.read_text(encoding='utf-8'));source=load(request)
    from harness.e2e import evaluator
    from ctxpress.harness import milestone_images,eval_plan,milestone_version,task_resources
    assert Path(evaluator.__file__).resolve()==source/'harness/e2e/evaluator.py'
    calls=[];observed={};repo='fixture_repo';mid='M2';base='sha256:'+'1'*64;agent='sha256:'+'2'*64;effective='sha256:'+'3'*64
    def inspect(*command):
        assert command[:2]==('image','inspect'),command
        calls.append(command)
        if command[2] not in observed:raise ValueError('prepared fixture alias missing')
        return json.dumps([observed[command[2]]])
    milestone_images.docker=inspect
    def image(value,labels=None):return dict(Id=value,Os='linux',Config={'Labels':labels or {}})
    observed[agent]=image(agent);observed[base]=image(base)
    plain=milestone_images.PreparedImages(evaluator,repo,{'id':agent},{mid:{'id':base}})
    assert plain.overlay(repo_name=repo,milestone_id=mid,milestone_image=base,quarantine_config=None)==(base,base[7:],'',base[7:])
    cases=[]
    for go in ('','1.25.0'):
        replace=['/usr/local/go'] if go else []
        policy=dict(closure=dict(cache_paths=['/wheelhouse'],toolchain={'go':go} if go else {}))
        recipe=dict(schema_version=evaluator.OFFLINE_CACHE_OVERLAY_SCHEMA_VERSION,cache_paths=['/wheelhouse'],replace_paths=replace,expected_go_toolchain=go)
        digest=hashlib.sha256(json.dumps(recipe,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        labels={evaluator.OFFLINE_CACHE_LABEL_PREFIX+'.'+key:value for key,value in {
            'schema':str(evaluator.OFFLINE_CACHE_OVERLAY_SCHEMA_VERSION),'milestone-image-id':base[7:],
            'closure-image-id':agent[7:],'policy-sha256':digest}.items()}
        alias=evaluator.local_ref(repo,mid+'-eval-closure',base[7:19]+'-'+agent[7:19]+'-'+digest[:12])
        observed[effective]=image(effective,labels);observed[alias]=observed[effective]
        ready=milestone_images.PreparedImages(evaluator,repo,{'id':agent},{mid:{'id':effective,'reference':alias}})
        actual=ready.overlay(repo_name=repo,milestone_id=mid,milestone_image=ready.resolve(evaluator.local_ref(repo,mid)),
            quarantine_config=policy,expected_closure_image_id=agent)
        assert actual==(effective,base[7:],agent[7:],effective[7:])
        observed[effective]['Config']['Labels'][evaluator.OFFLINE_CACHE_LABEL_PREFIX+'.policy-sha256']='0'*64
        try:ready.overlay(repo_name=repo,milestone_id=mid,milestone_image=base,quarantine_config=policy)
        except RuntimeError:pass
        else:raise AssertionError('stale native overlay policy was accepted')
        for command in (['docker','build','--pull=false','-'],['docker','tag',base,'new-alias']):
            try:ready.run(command)
            except ValueError:pass
            else:raise AssertionError('native image preparation was allowed')
        cases.append('go-toolchain-clean-replacement' if go else 'cache-overlay')
    from ctxpress.harness import milestone_containers
    from ctxpress.benchmarks import milestone_codex
    from harness.e2e import container_setup,orchestrator,run_e2e
    from harness.e2e.repo_config_binding import resolve_repo_config,freeze_repo_config
    from harness.e2e.runtime_policy_binding import resolve_runtime_policy,freeze_runtime_policy
    prepared=Path(request['folder'])/'native-preparation';workspace=prepared/'native-inputs'/request['task']['id'];trial=prepared/'trial'
    lock,_=task_resources.read(request['resources'],'swe-milestone',[request['original_task']])
    proof=lock['native_data_version'];milestone_version.materialize(proof,workspace.parent)
    from harness.e2e import data_version
    with milestone_version.pinned_environment(lock['release']):
        verified=data_version.check_data_version(workspace,context='offline frozen fixture')
    assert verified['data_version']['checked'] and verified['data_version']['state']=='match'
    assert verified['data_version']['commit']==proof['head']
    repo=workspace.name;config=freeze_repo_config(trial,resolve_repo_config(repo,workspace,project_root=source))
    policy=freeze_runtime_policy(trial,resolve_runtime_policy(repo,source));run_e2e._activate_runtime_policy(policy)
    class Owner:
        def __init__(self,registry,role,value):
            self.registry=registry;self.record=dict(container=registry.project+'-'+role+'-'+str(len(registry.owners)),
                role=role,image=value,phase='prepared',cleaned=False)
        def existing(self):return None
        def create(self,**options):
            self.options=options;self.record.update(phase='running',container_id='synthetic-only');return self.record['container']
        def cleanup(self):self.record.update(cleaned=True,phase='stopped')
        def persist(self):pass
        def inspect(self):return dict(HostConfig={'NetworkMode':'none'})
        def credentials(self,source):self.auth_source=source
    class Registry:
        project='ctxp-ms-'+'1'*24
        def __init__(self):self.owners=[];self.native_trials=set()
        def protect_trial(self,container):
            assert container not in self.native_trials
            self.native_trials.add(container)
        def release_trial(self,record):
            assert record['watcher_joined'] and record['author_cleanup_returned']
            self.native_trials.remove(record['container'])
        def owner(self,role,value):
            instance=Owner(self,role,value);self.owners.append(instance);return instance
        def grading_network(self):return self.project+'-grade'
        def service_gateway(self,owner):
            class Services:
                def __init__(self):
                    directory=prepared/('synthetic-service-api-'+str(len(owner.registry.owners)));directory.mkdir()
                    self.record=dict(channel=str(directory),scope='a'*24,verifier=owner.record['container'],phase='serving')
                    self.cleaned=False
                def stop(self):self.stopped=True
                def cleanup(self):
                    self.stop();assert owner.record['cleaned'];self.cleaned=True
                    Path(self.record['channel']).rmdir()
            return Services()
    registry=Registry();original=evaluator.PatchEvaluator;original_orchestrator=orchestrator.E2EOrchestrator
    grades={value:{'id':base} for value in request['task']['evaluation']['active_milestones']}
    from harness.e2e.agents.codex import CodexFramework
    private_settings=dict(runtime=request['package'],bindir=str(prepared/'unused-bin'),
        logs=str(prepared/'private-logs'),store=str(prepared/'private-store'),channel=str(prepared/'private-channel'),
        method={'class':'NoCompaction'},binary_version='0.159.0-alpha.12.1',compact_limit=230000,
        private_runtime=True,upstream='https://fixture.invalid/responses')
    private=milestone_codex.framework(CodexFramework,private_settings)
    private.set_model_relay('http://127.0.0.1:31234')
    initialized=[]
    def initialize(owner,framework):
        assert owner.record['private_initialized'] and framework.ctxpress_private_runtime
        initialized.append(owner)
    with contextlib.redirect_stdout(io.StringIO()), milestone_containers.installed(source,registry,repo,{'id':agent},grades,initialize) as hooks:
        native=run_e2e.E2EOrchestrator(repo_name=repo,milestone_version='v1',image_name=agent,
            dag_path=workspace/'dependencies.csv',srs_root=workspace/'srs',trial_root=trial,workspace_root=workspace,
            repo_src_dirs=['src'],test_dirs=['tests'],exclude_patterns=[],agent_name='codex',model='synthetic-model',
            config_path=trial/'e2e_config.yaml',repo_config_binding=config,runtime_policy_binding=policy)
        assert native.container_name==native.container_setup.ctxpress_owner.record['container']
        assert native.dag.all_milestones==set(grades)
        try:native.container_setup.start_container()
        except ValueError as error:assert 'private Codex/socket' in str(error)
        else:raise AssertionError('native Agent started without private model transport')
        # Run the actual Agent start body as well as the evaluator start. Its
        # Docker/user/runtime gates are substitutes; the option shape and call
        # order are native code, including the author's combined --add-host.
        setup=native.container_setup;setup._framework=private();gates=[]
        setup._ensure_python3=lambda:gates.append('tools')
        setup._wait_for_fakeroot=lambda:gates.append('user')
        setup.verify_runtime_environment=lambda:gates.append('runtime')
        inspect_original=container_setup.inspect_docker_image_id;run_original=subprocess.run
        def init(command,**options):
            assert command[:3]==['docker','exec',setup.container_name] and '/ctxpress-private' in command[-1]
            gates.append('init');return subprocess.CompletedProcess(command,0,'','')
        try:
            container_setup.inspect_docker_image_id=lambda image,**kw:agent[7:]
            subprocess.run=init;setup.start_container()
        finally:container_setup.inspect_docker_image_id=inspect_original;subprocess.run=run_original
        assert gates==['tools','init','user','runtime'] and initialized==[setup.ctxpress_owner]
        assert setup.ctxpress_owner.options['environment']['HOME']=='/root'

        from ctxpress.harness import milestone_agent,milestone_budget
        from harness.e2e import agent_runner
        budget=milestone_budget.Budget(prepared/'private-logs/sessions',30,10)
        docker_original=milestone_agent.docker;invocations=[]
        milestone_agent.docker=lambda *command:invocations.append(command)
        try:
            with milestone_codex.installed(private_settings) as registered:
                registered.set_model_relay('http://127.0.0.1:31234')
                original_runner=agent_runner.E2EAgentRunner
                original_trial=run_e2e.E2ETrialRunner
                with milestone_agent.installed(source,setup.ctxpress_owner,prepared/'synthetic-auth',budget) as Runner:
                    runner=Runner(container_name=setup.container_name,output_dir=str(prepared/'runner-check'),
                        agent_name='codex',model='synthetic-model',timeout_ms=60000,prompt_version='v2')
                    def command(args,check=True):
                        if args[:2]==['docker','cp']:raise RuntimeError('synthetic copy interruption')
                        output=setup.container_name if args[1]=='ps' else 'true'
                        return subprocess.CompletedProcess(args,0,output,'')
                    runner._run_command=command
                    assert runner.run(prompt='synthetic prompt',session_id='synthetic-session') is False
                    assert runner.timeout_ms==60000 and runner.resume_session('synthetic-session','synthetic message') is False
                    assert runner._refresh_codex_credentials()
                    assert setup.ctxpress_owner.auth_source==prepared/'synthetic-auth'
                    budget.reason='max_calls'
                    try:runner.resume_session('synthetic-session','must not start')
                    except milestone_budget.Limit:pass
                    else:raise AssertionError('native resumed Agent reset its tool budget')
                    trial_runner=run_e2e.E2ETrialRunner(orchestrator=native,agent_output_dir=prepared/'trial-runner-check',
                        workdir='/testbed',repo_src_dirs=['src'],agent_name='codex',model='synthetic-model',
                        timeout_ms=60000,prompt_version='v2',copy_testbed=False,remove_container=False)
                    try:trial_runner.run_agent_with_recovery(resume_session_first=True)
                    except milestone_budget.Limit:pass
                    else:raise AssertionError('native trial recovery reset its cumulative budget')
                    from ctxpress.harness import milestone_trial
                    with milestone_trial.installed(source,setup.ctxpress_owner,2):
                        drained=run_e2e.E2ETrialRunner(orchestrator=native,agent_output_dir=prepared/'drain-check',
                            workdir='/testbed',repo_src_dirs=['src'],agent_name='codex',model='synthetic-model',
                            timeout_ms=60000,prompt_version='v2',copy_testbed=False,remove_container=True)
                        drained._extract_agent_stats=lambda:None
                        # Execute the actual watcher startup and loop with a
                        # pre-set stop event: no tags, grader or Docker work.
                        drained.watcher_stop_event.set();drained.start_watcher_thread()
                        drained.watcher_thread.join(2)
                        assert not drained.watcher_thread.is_alive() and drained._watcher_exited_clean
                        drained.cleanup()
                        assert drained.remove_container and not registry.native_trials
                        evidence=json.loads((trial/'ctxpress-drain.json').read_text(encoding='utf-8'))
                        assert evidence['watcher_joined'] and evidence['author_cleanup_returned']
                assert agent_runner.E2EAgentRunner is original_runner and run_e2e.E2EAgentRunner is original_runner
                assert run_e2e.E2ETrialRunner is original_trial
        finally:milestone_agent.docker=docker_original
        assert len(invocations)==2 and all('/ctxpress-private/agent.json' in call for call in invocations)
        assert registry.owners[0].record['phase']=='running' and registry.owners[0].record['private_initialized']
        check=evaluator.PatchEvaluator(workspace_root=workspace,milestone_id='M2',patch_file=prepared/'synthetic.patch',
            baseline_classification=workspace/'test_results/M2/M2_classification.json',output_dir=prepared/'container-check',
            repo_config_path=config.path,repo_config_sha256=config.sha256,runtime_policy_path=policy.path,
            runtime_policy_sha256=policy.sha256,runtime_policy_mode=policy.mode)
        gates=[]
        for name in ('_install_jest_ipc_guard','_verify_evaluator_go_toolchain','_verify_evaluator_cache_policy'):
            setattr(check,name,lambda name=name:gates.append(name))
        check.start_container()
        assert len(gates)==3 and check.ctxpress_owner.record['phase']=='running'
        assert check._eval_meta['offline_cache_effective_image_id']==base[7:]
        assert check.ctxpress_owner.options['mounts'][0][1]=='/output'
        def invalid():raise RuntimeError('synthetic parity failure')
        failed=evaluator.PatchEvaluator(workspace_root=workspace,milestone_id='M2',patch_file=prepared/'synthetic.patch',
            baseline_classification=workspace/'test_results/M2/M2_classification.json',output_dir=prepared/'parity-check',
            repo_config_path=config.path,repo_config_sha256=config.sha256,runtime_policy_path=policy.path,
            runtime_policy_sha256=policy.sha256,runtime_policy_mode=policy.mode)
        failed._install_jest_ipc_guard=invalid
        try:failed.start_container()
        except RuntimeError:pass
        else:raise AssertionError('native evaluator ignored a parity failure')
        assert failed.ctxpress_owner.record['cleaned']
        check.cleanup();assert check.ctxpress_owner.record['cleaned']
        (workspace/'dockerfiles/M2/test_config.json').write_text(json.dumps([dict(name='e2e',test_states=['end'],
            test_cmd='synthetic-only',framework='playwright',requires_docker_socket=True)]), encoding='utf-8')
        services=evaluator.PatchEvaluator(workspace_root=workspace,milestone_id='M2',patch_file=prepared/'synthetic.patch',
            baseline_classification=workspace/'test_results/M2/M2_classification.json',output_dir=prepared/'service-check',
            repo_config_path=config.path,repo_config_sha256=config.sha256,runtime_policy_path=policy.path,
            runtime_policy_sha256=policy.sha256,runtime_policy_mode=policy.mode)
        assert services.needs_docker_socket and services.ctxpress_services is not None
        for name in ('_install_jest_ipc_guard','_verify_evaluator_go_toolchain','_verify_evaluator_cache_policy'):
            setattr(services,name,lambda:None)
        services.start_container()
        options=services.ctxpress_owner.options
        assert options['service_gateway'] is services.ctxpress_services
        assert options['environment']['DOCKER_HOST']=='unix:///ctxpress-services-api/docker.sock'
        assert options['environment']['TESTCONTAINERS_HOST_OVERRIDE']=='127.0.0.1'
        assert any(target=='/ctxpress-services-api' for _,target,_ in options['mounts'])
        assert not any(target=='/var/run/docker.sock' for _,target,_ in options['mounts'])
        services.cleanup();assert services.ctxpress_services.cleaned
        fake=object.__new__(container_setup.ContainerSetup);fake.agent_name='codex'
        initialization=milestone_containers.prepared_initialization(container_setup.ContainerSetup.__mro__[1]._get_base_init_script(fake))
        assert 'apt-get' not in initialization and 'apk' not in initialization and 'gitconfig' in initialization
    assert evaluator.PatchEvaluator is original and orchestrator.E2EOrchestrator is original_orchestrator
    native_agent=private(reasoning_effort='low')
    assert native_agent.ctxpress_private_runtime and not any('/tmp/host-codex' in value for value in native_agent.get_container_mounts())
    for command in (native_agent.build_run_command('fixture',None,'/tmp/prompt'),native_agent.build_resume_command('fixture','thread','/tmp/message')):
        values=shlex.split(command)
        assert 'ctxpress.harness.agent_process' in values and values[values.index('--via')+1]=='http://127.0.0.1:31234'
    result=dict(test_only=True,author_prepared_overlay_verified=True,cases=['no-overlay',*cases],
        author_testcontainers_launch_projection_verified=True,
        author_frozen_data_version_verified=True,
        author_policy_hash_verified=True,stale_policy_rejected=True,image_build_and_tag_blocked=True,
        author_container_start_projection_verified=True,native_orchestrator_name_bound=True,
        private_agent_runtime_required=True,post_start_gates_stubbed=True,author_parity_failure_cleanup_verified=True,
        author_private_codex_commands_verified=True,
        author_agent_start_projection_verified=True,author_agent_runtime_hook_verified=True,
        author_trial_recovery_budget_gate_verified=True,
        author_trial_watcher_start_stop_verified=True,author_trial_cleanup_returned=True,
        synthetic_image_inspections=len(calls),containers_started=0,model_calls=0,real_run_verified=False)
    eval_plan.atomic_json(args.output,result);print(json.dumps(result))


if __name__=='__main__':main()
