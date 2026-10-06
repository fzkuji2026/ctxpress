"""Author PolyBench evaluation with owned prepared images and no retrieval fetches."""
from __future__ import annotations
import inspect, os, threading
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch
from .swe_containers import check_repo


def repository_directory(request,client):
    directories=[]
    for field in ('agent_image','grading_image'):
        image=client.images.get(request[field])
        if image.id!=request[field] or image.attrs.get('Os')!='linux':
            raise ValueError('PolyBench requires pinned prepared Linux images')
        directory=(image.attrs.get('Config') or {}).get('WorkingDir')
        if (not isinstance(directory,str) or not directory.startswith('/') or directory=='/' or
                PurePosixPath(directory).as_posix()!=directory or '..' in PurePosixPath(directory).parts):
            raise ValueError('PolyBench image must declare an absolute repository WorkingDir')
        directories.append(directory)
    if len(set(directories))!=1:raise ValueError('PolyBench Agent and verifier repository WorkingDir differ')
    return directories[0]


def grade(request,module,client,instance,owner,agent_owner,prediction):
    if not agent_owner.record['cleaned'] or agent_owner.record['credentials_may_exist']:
        raise RuntimeError('PolyBench grading requires checked Agent cleanup')
    if prediction['instance_id']!=request['task']['id'] or instance.instance_id!=request['task']['id']:
        raise ValueError('PolyBench prediction/instance identity changed')
    root=Path(request['folder'])/'official-logs';root.mkdir(exist_ok=True)
    report=root/(instance.instance_id+'_result.json')
    if report.exists():raise ValueError('official PolyBench grading report cache is not fresh')
    directory=request['repository_directory'];threads=[];test_patch_failures=[]
    instance=instance.model_copy(update={'model_patch':prediction['model_patch']})
    original=module.DockerManager
    # Methods inherited from the author retain their actual module globals.
    # Track only the author's test threads, not worker/channel threads.
    docker_module=inspect.getmodule(original)
    if docker_module is None or not hasattr(docker_module,'threading'):
        raise ValueError('unsupported PolyBench DockerManager thread protocol')
    def tracked_thread(*args,**kwargs):
        kwargs['daemon']=True
        thread=threading.Thread(*args,**kwargs);threads.append(thread);return thread
    tracked=SimpleNamespace(**{key:getattr(docker_module.threading,key) for key in dir(docker_module.threading)})
    tracked.Thread=tracked_thread
    def disabled(*args,**kwargs):raise ValueError('PolyBench runtime image preparation/retrieval is disabled')
    class OwnedManager(original):
        def __init__(self,image_id,delete_image,client):
            if client is not owner.client or delete_image or image_id!='polybench_'+instance.language.lower()+'_'+instance.instance_id.lower():
                raise ValueError('PolyBench grader requested another image/client')
            super().__init__(image_id=image_id,delete_image=False,client=client)
        def check_image_local(self,local_image_name):
            if local_image_name!=self.image_id:raise ValueError('PolyBench requested an undeclared local image')
            image=self.client.images.get(owner.record['image'])
            if image.id!=owner.record['image']:raise ValueError('prepared PolyBench verifier image changed')
            return True
        try_pull_prebuilt_image=docker_build=build_base_image=repair_native_imports=disabled
        def _get_workdir_from_image(self):return directory
        def create_container(self):
            self.container=owner.create(user='root',working_dir=directory)
            self.container.start();check_repo(self.container,instance.base_commit,directory)
            owner.record['phase']='running';owner.persist()
        def apply_patch_to_container(self,patch_content,patch_type):
            try:
                result=super().apply_patch_to_container(patch_content,patch_type)
                if patch_type=='test' and result!=0:test_patch_failures.append('nonzero')
                return result
            except Exception:
                if patch_type=='test':test_patch_failures.append('exception')
                raise
        def _cleanup(self):
            if not owner.record['cleaned']:owner.cleanup(self.container)
            self.container=None
        def __del__(self):pass  # Checked cleanup belongs to the worker, never GC.
    original_store=module.store_instance_level_output
    def store(instance_output,result_path,suffix='_result'):
        if suffix=='_result':return original_store(instance_output,result_path,suffix)
        if suffix!='_metrics':raise ValueError('unknown PolyBench result kind')
        # Retrieval needs reference source checkout/tree-sitter. Omit this
        # optional metric rather than making fake zero retrieval scores.
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(module,'DockerManager',OwnedManager))
            stack.enter_context(patch.object(docker_module,'threading',tracked))
            stack.enter_context(patch.object(module,'instance_level_metric_scoring',lambda *a,**k:None))
            stack.enter_context(patch.object(module,'store_instance_level_output',store))
            if hasattr(module,'RepoManager'):stack.enter_context(patch.object(module,'RepoManager',disabled))
            if 'grading_timeout' in request['run']:
                for field in ('JAVA_TIMEOUT','DEFAULT_TIMEOUT'):
                    stack.enter_context(patch.object(module,field,request['run']['grading_timeout']))
            # evaluate_instance writes run_logs_<language> relative to cwd.
            # The isolated worker owns one trial; keep those artifacts private.
            previous=os.getcwd();stack.callback(os.chdir,previous);os.chdir(root)
            module.evaluate_instance(instance=instance,result_path=str(root),evaluate_gold=False,
                repo_path=str(root/'retrieval-disabled'),delete_image=False,client=client,
                retrieval_metrics_only=False,node_retrieval_metrics=False,repair_native_packages=None)
        if test_patch_failures:raise RuntimeError('PolyBench author test patch failed; grading infrastructure is invalid')
    finally:
        try:
            if not owner.record['cleaned']:owner.cleanup()
        finally:
            for thread in threads:
                if thread.ident is not None:thread.join(timeout=10)
            if any(thread.is_alive() for thread in threads):
                raise RuntimeError('PolyBench test IO thread did not finish after verifier removal')
    return report
