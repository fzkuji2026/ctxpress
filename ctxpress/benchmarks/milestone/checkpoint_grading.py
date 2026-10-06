"""Declared official SWE-Milestone grading inputs, isolated from live checkouts."""
from __future__ import annotations
import hashlib, importlib.util, json, os, re, subprocess, sys
from pathlib import Path
from ctxpress.harness.jobs import environment as eval_environment, plan as eval_plan

SCHEMA = 'ctxpress.eval.grading'
FOLDERS = {'code':'code', 'trials':'trials', 'yaml':'deps/yaml', 'pathspec':'deps/pathspec'}


def _seal(content):
    return dict(content,sha256=hashlib.sha256(eval_plan.canonical(content).encode()).hexdigest())


def tree(root, selections):
    """A declared subset, retaining original relative paths and empty directories."""
    root = Path(root).expanduser().resolve()
    files, directories, executables = {}, set(), set()
    for relative in selections:
        relative = eval_environment._relative(relative)
        path = root / relative
        if path.is_symlink():
            raise ValueError('grading inputs cannot be symbolic links')
        for parent in [relative.parent,*relative.parents]:
            if parent != Path('.'):
                directories.add(parent.as_posix())
        if path.is_dir():
            child = eval_environment.workspace(path)
            directories.add(relative.as_posix())
            directories.update((relative / name).as_posix() for name in child['directories'])
            files.update({(relative / name).as_posix():digest for name,digest in child['files'].items()})
            executables.update((relative / name).as_posix() for name in child['executables'])
        else:
            files[relative.as_posix()] = eval_plan.file_sha256(path)
            if path.stat().st_mode & 0o111:
                executables.add(relative.as_posix())
    return eval_environment.verify_tree(dict(root=str(root),files=files,directories=sorted(directories),executables=sorted(executables)))


def dependencies():
    result = {}
    for name in ('yaml','pathspec'):
        spec = importlib.util.find_spec(name)
        if spec is None or not spec.submodule_search_locations:
            raise ValueError('grading capture needs the installed '+name+' package; no automatic installation')
        result[name] = eval_environment.workspace(next(iter(spec.submodule_search_locations)))
    return result


def capture(code, data, trials, coordinates):
    from ctxpress.benchmarks.milestone.checkpoint_run import POINTS
    import yaml
    code,data,trials = (Path(path).expanduser().resolve() for path in (code,data,trials))
    version = (code/'manifests/BENCHMARK_VERSION').read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'v\d+\.\d+(?:\.\d+)?',version):
        raise ValueError('official benchmark version must be explicit')
    records, images, dataset, trial_files = {}, {}, {'metadata.json'}, set()
    for n,j in coordinates:
        key = f'{n}:{j}'
        if key in records or (n,j) not in POINTS:
            raise ValueError('provide unique boundaries with an official grading mapping')
        milestone,trial,_ = POINTS[(n,j)]
        repo = f'{trial}/repo_config.yaml'
        policy = f'{trial}/runtime_policy.yaml'
        template = f'{trial}/evaluation/{milestone}/source_snapshot.integrity.json'
        classification = f'test_results/{milestone}/{milestone}_classification.json'
        records[key] = dict(milestone=milestone,trial=trial,repo_config=repo,runtime_policy=policy,
                            template=template,classification=classification)
        trial_files.update((repo,policy,template))
        metadata = json.loads((trials/template).read_text(encoding='utf-8'))
        closure_id = metadata.get('agent_base_image_id')
        if isinstance(closure_id,str) and re.fullmatch(r'[0-9a-f]{64}',closure_id):
            closure_id = 'sha256:'+closure_id
        if not isinstance(closure_id,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',closure_id):
            raise ValueError('official capture template must bind its agent closure image ID')
        images['captured-closure:'+closure_id] = eval_environment.image(closure_id)
        dataset.update((f'dockerfiles/{milestone}',classification))
        filter_path = f'test_results/{milestone}/{milestone}_filter_list.json'
        if (data/filter_path).is_file():
            dataset.add(filter_path)
        config = yaml.safe_load((trials/repo).read_text(encoding='utf-8'))
        if config.get('evaluation_post_snapshot_script'):
            dataset.add(config['evaluation_post_snapshot_script'])
        for suffix in (milestone,'base-offline'):
            image_base = f'swe-milestone/{data.name.lower()}__{suffix.lower()}'
            if image_base not in images:
                images[image_base] = eval_environment.image(image_base+':'+version)
    if not records:
        raise ValueError('provide at least one official grading boundary')
    selections = ['harness','manifests','quarantine_configs','LICENSE','pyproject.toml']
    trees = dict(code=tree(code,selections),data=tree(data,sorted(dataset)),trials=tree(trials,sorted(trial_files)),**dependencies())
    runtime = dict(python=str(Path(sys.executable).resolve()),sha256=eval_plan.file_sha256(sys.executable),
                   version=sys.version,platform=sys.platform)
    return _seal(dict(schema=SCHEMA,version=1,trees=trees,images=images,boundaries=records,runtime=runtime,
                      repo_name=data.name,scope='copied official code, inputs, yaml/pathspec packages and pinned image IDs; host Python and system libraries remain live'))


def verify(lock, coordinates=()):
    content = {key:value for key,value in lock.items() if key!='sha256'}
    if lock.get('schema') != SCHEMA or type(lock.get('version')) is not int or lock['version'] != 1 or _seal(content)['sha256'] != lock.get('sha256'):
        raise ValueError('official grading manifest changed or is unsupported')
    if set(lock['trees']) != {'code','data','trials','yaml','pathspec'}:
        raise ValueError('grading manifest needs code, dataset, trials and both dependency trees')
    eval_environment._relative(lock['repo_name'])
    if len(Path(lock['repo_name']).parts) != 1:
        raise ValueError('grading repository name must be one path component')
    for descriptor in lock['trees'].values():
        eval_environment.verify_tree(descriptor)
    for key,record in lock['boundaries'].items():
        from ctxpress.benchmarks.milestone.checkpoint_run import POINTS
        try:
            coordinate = tuple(map(int,key.split(':')))
            expected = POINTS[coordinate]
        except (ValueError,KeyError):
            raise ValueError('grading manifest has no official boundary mapping') from None
        if (record['milestone'],record['trial']) != expected[:2]:
            raise ValueError('grading manifest differs from the official boundary mapping')
        for field,which in (('repo_config','trials'),('runtime_policy','trials'),('template','trials'),('classification','data')):
            eval_environment._relative(record[field])
            if record[field] not in lock['trees'][which]['files']:
                raise ValueError('grading boundary references an undeclared input')
    for n,j in coordinates:
        if f'{n}:{j}' not in lock['boundaries']:
            raise ValueError('grading manifest is missing a selected boundary')
    for record in lock['images'].values():
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',record['id']):
            raise ValueError('grading images require complete content IDs')
    runtime = lock['runtime']
    if not Path(runtime['python']).is_absolute() or not re.fullmatch(r'[0-9a-f]{64}',runtime['sha256']):
        raise ValueError('grading requires a declared host Python identity')
    return lock


def read(path,coordinates=()):
    path = Path(path).expanduser().resolve()
    if path.name.lower() == 'auth.json':
        raise ValueError('credential files cannot be grading manifests')
    raw = path.read_bytes()
    return verify(json.loads(raw),coordinates),hashlib.sha256(raw).hexdigest()


def load(path,coordinates=()):
    return read(path,coordinates)[0]


def folder(lock,key):
    return 'data/'+lock['repo_name'] if key=='data' else FOLDERS[key]


def relative(lock,source):
    for key,descriptor in lock['trees'].items():
        try:
            name = Path(source).relative_to(descriptor['root']).as_posix()
        except ValueError:
            continue
        if name in descriptor['files']:
            return 'grading/'+folder(lock,key)+'/'+name
    return None


def verify_copies(lock,root):
    verify(lock)
    root = Path(root).resolve()
    for key,descriptor in lock['trees'].items():
        actual = eval_environment.workspace(root/folder(lock,key))
        if not eval_environment.same_tree(actual, descriptor):
            raise ValueError('official grading input copy changed: '+key)
    for record in lock['boundaries'].values():
        template = json.loads((root/folder(lock,'trials')/record['template']).read_text(encoding='utf-8'))
        digest = template.get('agent_base_image_id','')
        digest = 'sha256:'+digest if re.fullmatch(r'[0-9a-f]{64}',digest) else digest
        if lock['images'].get('captured-closure:'+digest,{}).get('id') != digest:
            raise ValueError('official capture template closure image is not declared')


def runtime(lock,root,n,j,check_images=True):
    verify(lock,[(n,j)])
    verify_copies(lock,root)
    if eval_plan.file_sha256(lock['runtime']['python']) != lock['runtime']['sha256']:
        raise ValueError('declared grading Python changed')
    if check_images:
        for record in lock['images'].values():
            if eval_environment.image(record['id'])['id'] != record['id']:
                raise ValueError('declared grading image is unavailable')
    roots = {key:str(Path(root).resolve()/folder(lock,key)) for key in lock['trees']}
    return roots,lock['boundaries'][f'{n}:{j}']


def command(lock,manifest,root,n,j):
    worker = Path(__file__).with_name('checkpoint_worker.py').resolve()
    return [lock['runtime']['python'],'-I','-S','-B',str(worker),'--manifest',str(manifest),'--root',str(root),'--n',str(n),'--j',str(j)]


def preflight(manifest,root,n,j):
    """Import copied official code before model use; no evaluator execution."""
    lock = load(manifest,[(n,j)])
    runtime(lock,root,n,j)
    process = subprocess.run([*command(lock,manifest,root,n,j),'check'],capture_output=True,text=True)
    if process.returncode:
        raise ValueError('frozen official grading preflight failed: '+process.stderr[-4000:])
    return json.loads(process.stdout)


def execute(manifest,root,n,j,snapshot,tag,commit,package,output):
    lock = load(manifest,[(n,j)])
    runtime(lock,root,n,j)
    common = command(lock,manifest,root,n,j)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    for operation,arguments in (
        ('package',['--snapshot',str(snapshot),'--tag',tag,'--commit',commit,'--package',str(package)]),
        ('grade',['--snapshot',str(Path(package)/'source_snapshot.tar'),'--output',str(output)])):
        # Version checks and copied input validation also run inside each child.
        process = subprocess.run([*common,operation,*arguments],env=env,capture_output=True,text=True)
        Path(output).parent.mkdir(parents=True,exist_ok=True)
        (Path(output).parent/(Path(output).stem+'-'+operation+'.log')).write_text(process.stdout+process.stderr,encoding='utf-8')
        if process.returncode:
            return dict(error='frozen official '+operation+' failed',infra_invalid=True,grading_manifest_sha256=lock['sha256'])
    if not Path(output).is_file():
        return dict(error='official evaluator did not produce a report',infra_invalid=True,grading_manifest_sha256=lock['sha256'])
    grade = json.loads(Path(output).read_text(encoding='utf-8'))
    result = dict(resolved=grade.get('resolved'),test_summary=grade.get('test_summary'),infra_invalid=grade.get('infra_invalid'),
                report=str(output),report_sha256=eval_plan.file_sha256(output),grading_manifest_sha256=lock['sha256'],grading_scope=lock['scope'],images=lock['images'],
                milestone_id=lock['boundaries'][f'{n}:{j}']['milestone'],graded_commit=commit)
    if grade.get('test_only'):
        result['test_only'] = True
    return result
