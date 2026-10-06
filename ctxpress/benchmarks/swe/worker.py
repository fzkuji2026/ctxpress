"""Isolated frozen SWE-bench worker; grading imports checked before resources."""
from __future__ import annotations
import argparse, asyncio, importlib.metadata, inspect, json, os, signal, sys, tempfile, threading
from pathlib import Path


def load(request):
    package=Path(__file__).resolve().parents[3];runtime=request['runtime']
    if (str(package)!=request['package'] or Path(sys.executable).resolve()!=Path(runtime['python']).resolve() or
            sys.version!=runtime['version'] or sys.platform!=runtime['platform'] or sys.platform!='linux'):
        raise ValueError('SWE-bench worker runtime differs from frozen Linux Python')
    official=Path(request['official'])
    framework={'polybench':'polybench','pro-v1':'pro_v1','codebench':'bigcodebench'}.get(request['api'],'swebench')
    source=official/framework
    sys.path[:0]=[str(package),str(source/'src') if framework=='polybench' else str(source),str(official/'dependencies')]
    from ctxpress.harness.jobs import plan as eval_plan
    from ctxpress.core import artifacts as artifact_io
    if eval_plan.file_sha256(sys.executable)!=runtime['sha256']:raise ValueError('SWE-bench Python binary changed')
    from ctxpress.harness.runtime.codex_agent import check_catalog
    check_catalog(request,probe=True)
    if request['api']=='codebench':
        import docker
        importlib.metadata.version('docker')
        from ctxpress.benchmarks.bigcode.grading import load as check_code
        check_code(dict(official=request['official'],package=request['package'],host_python=runtime['version']))
        from ctxpress.benchmarks.bigcode.protocol import instance
        return None,docker,instance(request['task'])
    if request['api']=='pro-v1':
        # V1's local path must never initialize optional Modal or host env files.
        previous=sys.modules.get('modal');present='modal' in sys.modules;sys.modules['modal']=None
        try:
            import swe_bench_pro_eval as module
        finally:
            if present:sys.modules['modal']=previous
            else:sys.modules.pop('modal',None)
        import docker
        from helper_code import image_uri
        if (Path(module.__file__).resolve()!=source/'swe_bench_pro_eval.py' or
                Path(image_uri.__file__).resolve()!=source/'helper_code/image_uri.py' or
                module.docker is not docker):raise ValueError('Pro V1 author module/dependencies differ from frozen inputs')
        required={'patch','sample','output_dir','dockerhub_username','scripts_dir','prefix','redo','block_network','docker_platform'}
        if not required<=set(inspect.signature(module.eval_with_docker).parameters):
            raise ValueError('unsupported frozen Pro V1 eval_with_docker API')
        for field in ('main','parse_args','assemble_workspace_files','create_entryscript','strip_binary_hunks',
                      'collect_outputs_local','get_dockerhub_image_uri'):
            if not callable(getattr(module,field,None)):raise ValueError('unsupported frozen Pro V1 author field: '+field)
        for library in ('pandas','docker','tqdm'):importlib.metadata.version(library)
        from ctxpress.benchmarks.pro.v1 import instance
        return module,docker,instance(request['task'])
    try:import dotenv
    except ModuleNotFoundError as error:
        if error.name!='dotenv':raise
        dotenv=None
    # The official utils imports load_dotenv. Only declared frozen inputs may
    # affect grading; never let it walk the host's directories for credentials.
    if dotenv is not None:dotenv.load_dotenv=lambda *args,**kwargs:False
    if request['api']=='polybench':
        from ctxpress.core import toml
        import poly_bench_evaluation
        from poly_bench_evaluation import run_evaluation as module
        from poly_bench_evaluation.polybench_data import PolyBenchInstance
        import docker
        declared=toml.load(source/'pyproject.toml')['project']
        if (Path(poly_bench_evaluation.__file__).resolve()!=source/'src/poly_bench_evaluation/__init__.py' or
                declared['name'].replace('-','_')!='poly_bench_evaluation' or
                importlib.metadata.version('poly_bench_evaluation')!=declared['version']):
            raise ValueError('PolyBench source and distribution metadata differ')
        required={'instance','result_path','evaluate_gold','repo_path','delete_image','client',
                  'retrieval_metrics_only','node_retrieval_metrics','repair_native_packages'}
        if not required<=set(inspect.signature(module.evaluate_instance).parameters):
            raise ValueError('unsupported frozen PolyBench evaluate_instance API')
        for field in ('DockerManager','instance_level_metric_scoring','store_instance_level_output','JAVA_TIMEOUT','DEFAULT_TIMEOUT'):
            if not hasattr(module,field):raise ValueError('unsupported frozen PolyBench grading field: '+field)
        manager=module.DockerManager
        for method,fields in {'__init__':{'image_id','delete_image','client'},
                'apply_patch_to_container':{'patch_content','patch_type'},'docker_run':{'test_command','timeout'},
                'check_image_local':{'local_image_name'},'create_container':set(),'_cleanup':set(),'_get_workdir_from_image':set()}.items():
            function=getattr(manager,method,None)
            if not callable(function) or not fields<=set(inspect.signature(function).parameters):
                raise ValueError('unsupported frozen PolyBench DockerManager method: '+method)
        if not callable(getattr(PolyBenchInstance,'model_copy',None)):
            raise ValueError('PolyBench requires its Pydantic v2 instance model')
        if request['task']['initial_state']['repo'] not in module.REPO_TO_PARSER_CLASS:
            raise ValueError('frozen PolyBench lacks the task repository parser')
        from ctxpress.benchmarks.polybench.protocol import prepared_instance
        return module,docker,prepared_instance(request['task'],PolyBenchInstance)
    import swebench
    if (Path(swebench.__file__).resolve()!=official/'swebench'/'swebench'/'__init__.py' or
            importlib.metadata.version('swebench')!=swebench.__version__):
        raise ValueError('SWE-bench official source and distribution metadata differ')
    from swebench.harness import run_evaluation as module
    import docker
    fields=set(inspect.signature(module.run_instance).parameters)
    required={'test_spec','pred','client','run_id','timeout'}
    if request['api']=='legacy':required|={'rm_image','force_rebuild'};create='build_container'
    elif request['api']=='prepared':create='create_container'
    else:raise ValueError('unsupported SWE-bench TestSpec API')
    if not required<=fields or not callable(getattr(module,create,None)) or not callable(getattr(module,'cleanup_container',None)):
        raise ValueError('frozen SWE-bench lacks the supported official run_instance API')
    from ctxpress.benchmarks.swe.protocol import prepared_spec
    spec=prepared_spec(request['task'],official,request['api'],module)
    return module,docker,spec


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('request');parser.add_argument('--check',action='store_true')
    args=parser.parse_args(argv);request=json.loads(Path(args.request).read_text(encoding='utf-8'))
    if request.get('schema')!='ctxpress.eval.swe_trial' or request.get('version')!=1:raise ValueError('invalid frozen SWE-bench request')
    module,docker,spec=load(request)
    if args.check:
        if request['api'] in ('pro-v1','codebench'):
            print(json.dumps(dict(imports='verified',framework='pro-v1-author' if request['api']=='pro-v1' else 'bigcodebench-host-docker',model_calls=0)));return
        framework='poly_bench_evaluation' if request['api']=='polybench' else 'swebench'
        print(json.dumps(dict(imports='verified',framework=framework,version=importlib.metadata.version(framework),model_calls=0)));return
    from ctxpress.harness.runtime import connect_proxy, socket_bridge
    from ctxpress.harness.jobs import plan as eval_plan
    from ctxpress.core import artifacts as artifact_io
    from ctxpress.benchmarks.swe import trial as swe_trial
    from ctxpress.harness.runtime.socket_bridge import cleanup_channel
    timeout=max(1800,request['run'].get('grading_timeout',3600 if request['api']=='pro-v1' else 1800)+60)
    client=docker.from_env(timeout=timeout);client._ctxpress_not_found=docker.errors.NotFound
    daemon_id=client.info()['ID']
    channel=Path(tempfile.mkdtemp(prefix=request['project']+'-channel-'));channel.chmod(0o755)
    artifact_io.atomic_json(channel/'owner.json',dict(project=request['project'],label=request['label']))
    server,thread=None,None
    async def execute():
        runner=swe_trial.run
        if request['api']=='codebench':
            from ctxpress.benchmarks.bigcode.trial import run as runner
        execution=asyncio.create_task(runner(request,module,client,spec,channel,daemon_id));loop=asyncio.get_running_loop()
        for action in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(action,execution.cancel)
        try:return await execution
        finally:
            for action in (signal.SIGTERM,signal.SIGINT):loop.remove_signal_handler(action)
    try:
        template=connect_proxy.make_server('127.0.0.1',0,[request['target']],via=request['via'])
        handler=template.RequestHandlerClass;template.server_close()
        server=socket_bridge.unix_server(channel/'model.sock',handler);(channel/'model.sock').chmod(0o666)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        asyncio.run(execute())
    finally:
        if server:
            if thread and thread.is_alive():server.shutdown();thread.join(timeout=5)
            server.server_close()
        path=Path(request['folder'])/'resources-swe-agent.json'
        if not path.exists() or json.loads(path.read_text(encoding='utf-8'))['cleaned']:
            cleanup_channel(dict(channel=str(channel),project=request['project'],label=request['label']))
        client.close()


if __name__=='__main__':main()
