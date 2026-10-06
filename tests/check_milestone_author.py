"""Explicit offline check against existing native code/dependencies; no Docker or models.

Run with a Linux Python and --code, --yaml, --pathspec pointing to existing
source/package directories. This only copies local inputs into a temporary
fixture, invokes the isolated preflight and saves its preparation evidence.
"""
import argparse,json,shutil,subprocess,sys,tempfile
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ctxpress import benchmarks
from ctxpress.benchmarks import milestone_driver
from ctxpress.harness import eval_environment,eval_inputs,eval_plan,eval_trees,evaluation,task,task_resources,milestone_version


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('code','output'):parser.add_argument('--'+name,required=True,type=Path)
    for name in ('yaml','pathspec'):parser.add_argument('--'+name,type=Path)
    parser.add_argument('--collector-only',action='store_true',help='only read synthetic scoring fixtures; no trial or container APIs')
    parser.add_argument('--pilot-grade',type=Path,help='existing failed native grade to recheck in collector-only mode')
    args=parser.parse_args()
    if args.collector_only:
        # -I omits this script's directory, so add it explicitly for the helper.
        sys.path.insert(0,str(Path(__file__).resolve().parent))
        from check_milestone_scoring import check
        result=check(args.code,args.pilot_grade)
        args.output.parent.mkdir(parents=True,exist_ok=True);eval_plan.atomic_json(args.output,result)
        print(json.dumps(result));return
    if args.yaml is None or args.pathspec is None:
        parser.error('--yaml and --pathspec are required unless --collector-only is used')
    with tempfile.TemporaryDirectory(prefix='ctxpress-native-frozen-') as temporary:
        root=Path(temporary);source=root/'code';source.mkdir()
        for name in ('harness','config','quarantine_configs','manifests'):
            if (args.code/name).is_dir():
                shutil.copytree(args.code/name,source/name,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        shutil.copy2(args.code/'pyproject.toml',source/'pyproject.toml')
        deps=root/'dependencies';deps.mkdir()
        for name in ('yaml','pathspec'):
            shutil.copytree(getattr(args,name),deps/name,symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        release=(source/'manifests/BENCHMARK_VERSION').read_text(encoding='utf-8').strip()
        data=root/'fixture_repo';data.mkdir()
        (data/'metadata.json').write_text(json.dumps(dict(repo_name='fixture/repo',base_tag='v1',repo_src_dirs=['src'],test_dirs=['tests'],exclude_patterns=[])), encoding='utf-8')
        (data/'dataset_manifest.json').write_text(json.dumps(dict(dataset='swe-milestone',revision=release)), encoding='utf-8')
        (data/'milestones.csv').write_text('id,title\nM1,first\nM2,second\nM3,third\n', encoding='utf-8')
        (data/'dependencies.csv').write_text('source_id,target_id,dependency_type\nM1,M2,strong\nM2,M3,strong\n', encoding='utf-8')
        (data/'selected_milestone_ids.txt').write_text('M1\nM2\nM3\n', encoding='utf-8')
        (data/'non-graded_milestone_ids.txt').write_text('M1\n', encoding='utf-8')
        for mid in ('M1','M2','M3'):
            for directory in ('srs','dockerfiles','test_results'):(data/directory/mid).mkdir(parents=True)
            (data/'srs'/mid/'SRS.md').write_text('Synthetic requirement.', encoding='utf-8')
            configuration=[dict(name='synthetic',test_states=['end'],test_cmd='synthetic-only',framework='playwright',requires_docker_socket=True)] if mid=='M2' else {}
            (data/'dockerfiles'/mid/'test_config.json').write_text(json.dumps(configuration), encoding='utf-8')
            (data/'test_results'/mid/(mid+'_classification.json')).write_text('{}', encoding='utf-8')
        found=benchmarks.get('swe-milestone').task_instances(data)[0]
        subprocess.run(['git','init','-q',str(data)],check=True)
        subprocess.run(['git','-C',str(data),'add','.'],check=True)
        identity=['git','-C',str(data),'-c','user.name=fixture','-c','user.email=fixture@example.invalid']
        subprocess.run(identity+['commit','-qm','synthetic native data fixture'],check=True)
        subprocess.run(identity+['tag','-am','synthetic release',release],check=True)
        proof=milestone_version.capture([found],release)
        trees={}
        for key,path in (('code',source),('dependencies',deps)):
            observed=eval_environment.workspace(path)
            trees[key]=eval_trees.capture(path,list(observed['files']),folder='unused',directories=observed['directories'])['tree']
        image='sha256:'+'a'*64
        runtime=dict(python=str(Path(sys.executable).resolve()),sha256=eval_plan.file_sha256(sys.executable),version=sys.version,platform=sys.platform)
        record=dict(task_sha256=task_resources.task_digest(found),images=dict(agent={'id':image,'reference':'fixture:'+release},grading={mid:{'id':image} for mid in ('M1','M2','M3')}))
        record['images']['services']={'homeserver':{'id':image,'reference':'ghcr.io/element-hq/synapse:develop@sha256:66955f34a593cfc3b6e77b8d5510c60c6094f5bade8a17d2feaefbb8662ccf09'}}
        lock=task_resources.seal(dict(schema=task_resources.SCHEMA,version=1,benchmark='swe-milestone',release=release,
            tasks={found['id']:record},trees=trees,runtime=runtime,native_data_version=proof))
        resources=root/'resources.json';resources.write_text(json.dumps(lock), encoding='utf-8')
        binary=root/'bin';binary.mkdir();(binary/'codex').write_bytes(b'not executed: synthetic CLI fixture')
        cfg=dict(schema='ctxpress.eval',version=1,benchmark='swe-milestone',start_mode='task_start',scope='mechanism',model='fixture-model',
            backend='codex_docker',tasks=[found['id']],methods=[{'class':'NoCompaction'}],
            environment=dict(data=str(data),bindir=str(binary),resources=str(resources)))
        plan=eval_plan.compile_plan(cfg)
        assert plan['missing_environment_files']==[],plan['missing_environment_files']
        assert plan['benchmark']['execution_supported'] and plan['benchmark']['official_grading_supported']
        assert not plan['benchmark']['real_run_verified']
        run=evaluation.prepare(plan,root/'run');config,paths=eval_inputs.execution(plan,run/'inputs');job=plan['jobs'][0]
        moved=task.remap(job['task'],paths)
        shutil.rmtree(source);shutil.rmtree(deps);shutil.rmtree(data);shutil.rmtree(binary);resources.unlink()
        actual=milestone_driver.preflight(moved,config,job,run/'attempt')
        assert actual['active_milestones']==['M1','M2','M3'] and actual['graded_milestones']==['M2','M3']
        assert actual['containers_started']==actual['model_calls']==0 and not actual['execution_supported']
        image_check=root/'image-check.json'
        subprocess.run([sys.executable,'-I','-S','-B',str(Path(__file__).with_name('check_milestone_images.py')),
            '--request',str(run/'attempt/milestone-check-request.json'),'--output',str(image_check)],
            env={},check=True,timeout=60)
        image_result=json.loads(image_check.read_text(encoding='utf-8'))
        assert image_result['author_prepared_overlay_verified'] and image_result['image_build_and_tag_blocked']
        assert image_result['author_trial_watcher_start_stop_verified'] and image_result['author_trial_cleanup_returned']
        assert image_result['author_frozen_data_version_verified']
        assert image_result['author_testcontainers_launch_projection_verified']
        lifecycle_check=root/'lifecycle-check.json'
        subprocess.run([sys.executable,'-I','-S','-B',str(Path(__file__).with_name('check_milestone_lifecycle.py')),
            '--request',str(run/'attempt/milestone-check-request.json'),'--output',str(lifecycle_check)],
            env={},check=True,timeout=60)
        lifecycle_result=json.loads(lifecycle_check.read_text(encoding='utf-8'))
        assert lifecycle_result['native_trial_composition_verified'] and lifecycle_result['drain_verified']
        timeout_check=root/'lifecycle-timeout-check.json'
        subprocess.run([sys.executable,'-I','-S','-B',str(Path(__file__).with_name('check_milestone_lifecycle.py')),
            '--request',str(run/'attempt/milestone-check-request.json'),'--output',str(timeout_check),'--case','timeout'],
            env={},check=True,timeout=60)
        timeout_result=json.loads(timeout_check.read_text(encoding='utf-8'))
        assert timeout_result['budget_timeout_verified'] and timeout_result['drain_verified']
        # Exercise the actual collector through the same isolated frozen worker.
        # A DAG marked completed is not a passing test result; retries count once.
        trial=root/'synthetic-trial';evaluation_dir=trial/'evaluation';evaluation_dir.mkdir(parents=True)
        counts=dict(total=2,passed=2,failed=0,error=0,skipped=0,fail_to_pass_required=1,
            fail_to_pass_achieved=1,none_to_pass_required=0,none_to_pass_achieved=0,
            pass_to_pass_required=1,pass_to_pass_achieved=1,pass_to_pass_failed=0,pass_to_pass_missing=0)
        summary=dict(repo_name=found['id'],agent_name='codex',total_milestones=3,results={
            'M1':dict(dag_status='completed',eval_status='passed',attempt=0,test_summary=counts),
            'M2':dict(dag_status='completed',eval_status='passed',attempt=0,test_summary=counts),
            'M3':dict(dag_status='completed',eval_status='passed',attempt=0,test_summary=counts),
            'M2-retry1':dict(dag_status='completed',eval_status='failed',attempt=1,test_summary=counts)})
        def write_json(path,value):
            path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value), encoding='utf-8')
        write_json(evaluation_dir/'summary.json',summary)
        for mid in ('M1','M2','M3'):
            write_json(evaluation_dir/mid/'evaluation_result.json',dict(milestone_id=mid,resolved=True,test_summary=counts))
        invalid=dict(counts,total=0,passed=0,fail_to_pass_achieved=0,pass_to_pass_achieved=0)
        write_json(evaluation_dir/'M3/evaluation_result.json',dict(milestone_id='M3',resolved=True,infra_invalid=True,test_summary=invalid))
        write_json(evaluation_dir/'M2-retry1/evaluation_result.json',dict(milestone_id='M2',resolved=False,test_summary=dict(counts,failed=1,passed=1)))
        write_json(evaluation_dir/'M2-retry1/evaluation_result_filtered.json',dict(milestone_id='M2',resolved=True,test_summary=counts))
        write_json(trial/'agent_stats.json',dict(summary=dict(total_cost_usd=1.25,total_turns=5,duration_ms=1000),
            modelUsage={'fixture':dict(outputTokens=10,reasoningOutputTokens=15)}))
        grade=milestone_driver.read_grade(moved,config,job,run/'grade-check',trial)
        assert grade['official_metrics']['graded']==2 and grade['official_metrics']['resolved']==1
        assert grade['official_metrics']['infra_invalid']==1 and grade['official_metrics']['output_tokens']==25
        assert grade['milestones']['M2']['served_attempt']=='M2-retry1' and grade['result_type_counts']=={'filtered':1,'unfiltered':2}
        assert grade['milestones']['M1']['resolved'] and grade['official_metrics']['resolved']==1
        assert grade['resolved'] is None and not grade['scoring_complete']
        summary['results']['M2-retry2']=dict(dag_status='completed',eval_status='error',attempt=2)
        write_json(evaluation_dir/'summary.json',summary)
        newer=milestone_driver.read_grade(moved,config,job,run/'newer-summary-check',trial)
        assert newer['milestones']['M2']['served_attempt'] is None and newer['official_metrics']['resolved']==0
        summary['results'].pop('M2-retry2');write_json(evaluation_dir/'summary.json',summary)
        write_json(evaluation_dir/'M3/evaluation_result.json',dict(milestone_id='M3',resolved=True,
            patch_status={'compilation_success':False},test_summary=invalid))
        build=milestone_driver.read_grade(moved,config,job,run/'build-failure-check',trial)
        assert build['scoring_complete'] and build['resolved'] is False and build['official_metrics']['infra_invalid']==0
        result=dict(test_only=True,author_preflight_verified=True,original_sources_removed=True,release=release,
            native_trial_composition=lifecycle_result,native_timeout_composition=timeout_result,
            author_testcontainers_launch_projection_verified=True,
            author_frozen_data_version_verified=True,
            official_code_files=len(trees['code']['files']),dependency_files=len(trees['dependencies']['files']),
            native_dag=actual['native_dag'],author_collector_verified=True,retry_selection_verified=True,
            filtered_result_verified=True,infra_vs_build_failure_verified=True,non_graded_denominator_verified=True,
            author_prepared_overlay_verified=True,image_build_and_tag_blocked=True,
            author_trial_watcher_start_stop_verified=True,author_trial_cleanup_returned=True,
            repo_config_sha256=actual['repo_config_sha256'],runtime_policy_sha256=actual['runtime_policy_sha256'],
            model_calls=0,containers_started=0,real_run_verified=False)
        args.output.parent.mkdir(parents=True,exist_ok=True);eval_plan.atomic_json(args.output,result)
        print(json.dumps(result))


if __name__=='__main__':main()
