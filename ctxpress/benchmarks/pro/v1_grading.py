"""Invoke Pro V1's actual main/scorer while binding Docker IO to owned images."""
from __future__ import annotations
import json, os
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from ctxpress.benchmarks.pro import v1 as pro_v1
from ctxpress.harness.jobs import plan as eval_plan
from ctxpress.benchmarks.swe.containers import check_repo


def grade(request,module,client,sample,owner,agent_owner,prediction):
    if not agent_owner.record['cleaned'] or agent_owner.record['credentials_may_exist']:
        raise RuntimeError('Pro V1 grading requires checked Agent cleanup')
    identity=request['task']['id']
    if prediction['instance_id']!=identity or sample['instance_id']!=identity:
        raise ValueError('Pro V1 prediction/instance identity changed')
    root=Path(request['folder'])/'official-logs';root.mkdir()
    dataset=root/'selected-instance.jsonl';dataset.write_text(json.dumps(sample)+'\n',encoding='utf-8')
    patches=root/'selected-patch.json';patches.write_text(json.dumps([prediction]),encoding='utf-8')
    workspace=root/identity/'workspace';timeout=request['run'].get('grading_timeout',3600)
    failures=[];executions=[];image_id=owner.record['image']
    def bound_uri(uid,username,repo=''):
        if uid!=identity or username!='ctxpress-prepared' or repo!=sample['repo']:
            raise ValueError('Pro V1 requested another image/task identity')
        return image_id
    class Images:
        def get(self,image,**kwargs):
            if image!=image_id or kwargs:raise ValueError('Pro V1 image drift/preparation is disabled')
            value=client.images.get(image)
            if value.id!=image_id:raise ValueError('Pro V1 prepared image changed')
            return value
        # The author's pull step resolves the already prepared content ID.
        # Nothing reaches a registry, even on an SDK failure or fallback.
        pull=get
    class Container:
        def wait(self):
            try:
                if executions:raise ValueError('Pro V1 verifier can execute only once')
                executions.append(True)
                result=owner.container.exec_run(['timeout','--signal=TERM','--kill-after=10s',str(timeout),
                    '/bin/bash','-c','bash /workspace/entryscript.sh'],workdir='/app',user='root')
                (root/'entryscript-exec.log').write_bytes(result.output or b'')
                if result.exit_code in (124,137):raise RuntimeError('Pro V1 official grading timed out')
                return {'StatusCode':result.exit_code}
            except Exception as error:failures.append(error);raise
    class Containers:
        def run(self,image,**kwargs):
            try:
                expected=dict(volumes={str(workspace):{'bind':'/workspace','mode':'rw'}},detach=True,remove=True,
                    entrypoint='/bin/bash',command=['-c','bash /workspace/entryscript.sh'],network_mode='none')
                if image!=image_id or kwargs!=expected:raise ValueError('Pro V1 requested unbound container resources')
                if owner.container is not None:raise ValueError('Pro V1 verifier is already allocated')
                container=owner.create(user='root',volumes=expected['volumes'],entrypoint=[],working_dir='/app')
                container.start();check_repo(container,sample['base_commit'],'/app')
                check=container.exec_run(['/bin/sh','-c','command -v timeout'],user='root')
                if check.exit_code:raise ValueError('Pro V1 prepared verifier requires timeout')
                owner.record.update(phase='running',base_commit=sample['base_commit']);owner.persist()
                return Container()
            except Exception as error:failures.append(error);raise
    bound=SimpleNamespace(images=Images(),containers=Containers())
    def from_env(*args,**kwargs):
        if args or kwargs:raise ValueError('Pro V1 attempted another Docker client')
        return bound
    original=module.eval_with_docker
    def checked_eval(patch_text,row,output_dir,dockerhub_username,scripts_dir,**kwargs):
        try:
            selected=row.to_dict() if hasattr(row,'to_dict') else dict(row)
            if (selected!=sample or patch_text!=prediction['model_patch'] or output_dir!=str(root) or
                    dockerhub_username!='ctxpress-prepared' or scripts_dir!=str(Path(request['official'])/'pro_v1/run_scripts') or
                    kwargs!=dict(prefix='',redo=True,block_network=True,docker_platform=None)):
                raise ValueError('Pro V1 author evaluated another submission or protocol')
            result=original(patch_text,row,output_dir,dockerhub_username,scripts_dir,**kwargs)
            if result is None:raise RuntimeError('Pro V1 official evaluator returned no parser output')
            pro_v1.validate_output(result)
            return result
        except Exception as error:failures.append(error);raise
    args=SimpleNamespace(raw_sample_path=str(dataset),patch_path=str(patches),output_dir=str(root),
        dockerhub_username='ctxpress-prepared',scripts_dir=str(Path(request['official'])/'pro_v1/run_scripts'),
        use_local_docker=True,docker_platform=None,redo=True,num_workers=1,block_network=True)
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(module,'docker',SimpleNamespace(from_env=from_env)))
            stack.enter_context(patch.object(module,'get_dockerhub_image_uri',bound_uri))
            stack.enter_context(patch.object(module,'eval_with_docker',checked_eval))
            stack.enter_context(patch.object(module,'parse_args',lambda:args))
            # A pinned content ID needs no host architecture heuristic.
            stack.enter_context(patch.object(module,'py_platform',SimpleNamespace(machine=lambda:'x86_64')))
            previous=os.getcwd();stack.callback(os.chdir,previous);os.chdir(Path(request['official'])/'pro_v1')
            module.main()  # Preserve the author's test-set comparison and verdict.
        if failures:raise RuntimeError('Pro V1 grading infrastructure is invalid') from failures[0]
        if executions!=[True]:raise RuntimeError('Pro V1 author did not run the owned verifier')
        report=root/'eval_results.json';raw=json.loads(report.read_text(encoding='utf-8'))
        if not isinstance(raw,dict) or set(raw)!={identity} or type(raw[identity]) is not bool:
            raise ValueError('Pro V1 author verdict is missing or ambiguous')
        parser_output=root/identity/'_output.json';pro_v1.validate_output(json.loads(parser_output.read_text(encoding='utf-8')))
    finally:
        if not owner.record['cleaned']:owner.cleanup()
    def artifact(path):return dict(path=str(path.resolve()),sha256=eval_plan.file_sha256(path))
    record=dict(schema=pro_v1.SCHEMA,version=1,benchmark_version='v1',task_id=identity,
        project=request['project'],base_commit=sample['base_commit'],report=artifact(report),
        prediction=artifact(Path(request['folder'])/'predictions.jsonl'),parser_output=artifact(parser_output),
        agent_image=agent_owner.record['image'],verifier_image=image_id,
        agent_resources_sha256=eval_plan.file_sha256(agent_owner.path),verifier_resources_sha256=eval_plan.file_sha256(owner.path),
        protocol='ctxpress_comparison',published_protocol_reproduced=False)
    eval_plan.atomic_json(root/'pro-v1-grade.json',record)
    return report
