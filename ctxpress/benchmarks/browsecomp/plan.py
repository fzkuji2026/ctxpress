"""BrowseComp-Plus plans: the pinned ACM author agent loop, a served model and a ctxpress method in between.

The agent is the author's research loop (search / get_document over the local BM25 corpus, optional
manage_context / query_memory). It calls its model through a ctxpress proxy that applies the planned method, so
usage, costs and method activity are logged like every other family. Compiling a plan reads and hashes local files
only: no download, model, index build or agent run.
"""
from __future__ import annotations
import copy, hashlib, json, os, re
from pathlib import Path
from urllib.parse import urlsplit

from ctxpress.harness.jobs import plan as eval_plan
from ctxpress.methods import build

ENVIRONMENT = {'checkout', 'python', 'data', 'ground_truth', 'qrels', 'index', 'agent_upstream', 'summarizer', 'grader'}
RUN = {'use_memory_tool', 'timeout', 'grade', 'retrieval_k', 'context_window', 'max_new_tokens'}
DEFAULT_RUN = dict(use_memory_tool=False, timeout=3600, grade=True)


def endpoint(value, name):
    url = urlsplit(value) if isinstance(value, str) else None
    if (url is None or url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or
            url.query or url.fragment):
        raise ValueError(name + ' must be an HTTP(S) base URL without credentials, query or fragment')
    return value.rstrip('/')


def regular(path, name):
    path = Path(path).expanduser().absolute()
    if path.name.lower() in ('auth.json', '.env') or not path.is_file():
        raise ValueError(name + ' must be an existing non-credential file')
    return path


def interpreter(path):
    """The author environment's interpreter; a virtualenv link is allowed and its target is what gets hashed."""
    path = Path(path).expanduser().absolute()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError('python must be the author environment interpreter')
    return path


def tree(root):
    """Every file under a prepared directory (the BM25 index), by relative path and SHA-256."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError('index must be a prepared local directory')
    files = {}
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('index must not contain symlinks')
        if path.is_file():
            files[path.relative_to(root).as_posix()] = eval_plan.file_sha256(path)
    if not files:
        raise ValueError('index directory is empty')
    return root, files


def questions(path):
    """The author's question-set format: a JSON list of {id | query_id, question | query, answer}."""
    rows = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(rows, list) or not rows:
        raise ValueError('question set must be a nonempty JSON list')
    found = {}
    for row in rows:
        identity = row.get('id', row.get('query_id')) if isinstance(row, dict) else None
        question = (row.get('question') or row.get('query')) if isinstance(row, dict) else None
        if not isinstance(identity, (str, int)) or not str(identity).strip() or not isinstance(question, str) or not question.strip():
            raise ValueError('each question needs an id and question text')
        if not isinstance(row.get('answer'), str):
            raise ValueError('each question needs its reference answer')
        identity = str(identity)
        if identity in found:
            raise ValueError('duplicate question ID: ' + identity)
        found[identity] = dict(id=identity, question=question, answer=row['answer'])
    return found


def service(value, name):
    if not isinstance(value, dict) or set(value) != {'model', 'upstream'} or not isinstance(value['model'], str) or not value['model'].strip():
        raise ValueError(name + ' needs exactly a model and an upstream')
    return dict(model=value['model'], upstream=endpoint(value['upstream'], name + '.upstream'))


def compile_plan(adapter, config, base_dir=None):
    config = copy.deepcopy(config)
    allowed = {'schema', 'version', 'name', 'scope', 'model', 'benchmark', 'start_mode', 'backend',
               'environment', 'tasks', 'methods', 'repeats', 'workers', 'run', 'prices'}
    if config.get('schema') != 'ctxpress.eval' or config.get('version') != 1 or set(config) - allowed:
        raise ValueError('unsupported BrowseComp-Plus evaluation configuration')
    if config.get('start_mode') != 'task_start' or config.get('scope') not in ('mechanism', 'benchmark'):
        raise ValueError('BrowseComp-Plus plans start from the question (task_start) with mechanism or benchmark scope')
    description = adapter.describe()
    config['benchmark'] = description['name']
    if config.get('backend') not in description['backends']:
        raise ValueError('BrowseComp-Plus runs with backend acm_author')
    model = config.get('model')
    if not isinstance(model, str) or not model.startswith('openai/') or any(c.isspace() for c in model):
        raise ValueError('model must be the LiteLLM name of the served agent model: openai/<served-name>')
    from ctxpress.live import usage as eval_usage
    eval_usage.validate_prices(config.get('prices'))
    base = Path(base_dir or os.getcwd()).resolve()
    environment = config.get('environment')
    if not isinstance(environment, dict) or set(environment) - ENVIRONMENT or ENVIRONMENT - {'qrels', 'grader'} - set(environment):
        raise ValueError('BrowseComp-Plus environment needs ' + ', '.join(sorted(ENVIRONMENT - {'qrels', 'grader'})) +
                         ' (qrels and grader optional)')
    local = lambda key: str((base / Path(environment[key]).expanduser()).absolute())
    for key in ('checkout', 'python', 'data', 'ground_truth', 'qrels', 'index'):
        if key in environment:
            environment[key] = local(key)
    from ctxpress.harness.author_acm import verify_checkout, REVISION, REPOSITORY
    checkout, sources = verify_checkout(environment['checkout'])
    environment['checkout'] = str(checkout)
    python = interpreter(environment['python'])
    data = regular(environment['data'], 'data')
    truth = regular(environment['ground_truth'], 'ground_truth')
    qrels = regular(environment['qrels'], 'qrels') if 'qrels' in environment else None
    index, index_files = tree(environment['index'])
    environment['index'] = str(index)
    environment['agent_upstream'] = endpoint(environment['agent_upstream'], 'agent_upstream')
    environment['summarizer'] = service(environment['summarizer'], 'summarizer')
    if 'grader' in environment:
        environment['grader'] = service(environment['grader'], 'grader')
    catalog = questions(data)
    identifiers = config.get('tasks')
    if (not isinstance(identifiers, list) or not identifiers or len(set(map(str, identifiers))) != len(identifiers) or
            any(not isinstance(value, (str, int)) for value in identifiers)):
        raise ValueError('provide a nonempty list of unique question IDs')
    identifiers = [str(value) for value in identifiers]
    unknown = set(identifiers) - set(catalog)
    if unknown:
        raise ValueError('unknown BrowseComp-Plus questions: ' + ', '.join(sorted(unknown)))
    settings = config.get('run') or {}
    if not isinstance(settings, dict) or set(settings) - RUN:
        raise ValueError('BrowseComp-Plus run settings: ' + ', '.join(sorted(RUN)))
    settings = dict(DEFAULT_RUN, **settings)
    if type(settings['use_memory_tool']) is not bool or type(settings['grade']) is not bool:
        raise ValueError('use_memory_tool and grade must be booleans')
    eval_plan._positive(settings['timeout'], 'timeout', integer=False)
    for key in ('retrieval_k', 'context_window', 'max_new_tokens'):
        if key in settings:
            eval_plan._positive(settings[key], key)
    if config['scope'] == 'benchmark' and not settings['grade']:
        raise ValueError('benchmark scope requires official grading')
    if settings['grade'] and 'grader' not in environment:
        raise ValueError('grading needs environment.grader (the author grades with an LLM judge)')
    config['run'] = settings
    config['repeats'] = eval_plan._positive(config.get('repeats', 1), 'repeats')
    config['workers'] = eval_plan._positive(config.get('workers', 1), 'workers')
    config['reasoning'] = config.get('reasoning', 'model-default')
    config['environment'] = environment

    artifacts = {str(python.resolve()): eval_plan.file_sha256(python.resolve())}
    for path in (data, truth, *([qrels] if qrels else [])):
        artifacts[str(path)] = eval_plan.file_sha256(path)
    for name, digest in sources.items():
        artifacts[str(checkout / name)] = digest
    for name, digest in index_files.items():
        artifacts[str(index / name)] = digest
    inputs = [dict(role='task', path=str(data), sha256=artifacts[str(data)]),
              dict(role='grading', path=str(truth), sha256=artifacts[str(truth)])]
    if qrels:
        inputs.append(dict(role='grading', path=str(qrels), sha256=artifacts[str(qrels)]))
    methods = config.get('methods')
    if not isinstance(methods, list) or not methods:
        raise ValueError('provide context methods (NoCompaction when the agent manages its own context)')
    labels, jobs = set(), []
    for number, entry in enumerate(methods):
        if not isinstance(entry, dict) or set(entry) - {'class', 'args', 'label'}:
            raise ValueError('invalid method entry')
        entry.setdefault('args', {})
        label = entry.get('label') or entry.get('class')
        if not isinstance(label, str) or not label or label in labels:
            raise ValueError('method labels must be unique')
        labels.add(label)
        method = build(entry)
        method.validate_live()
        if method.agent_tools:
            raise ValueError(label + ': methods with their own agent tools need a host that exposes them; '
                             'the author loop only offers its own tools')
        for position, identity in enumerate(identifiers):
            row = catalog[identity]
            task = adapter.task(row, inputs)
            for repeat in range(config['repeats']):
                jobs.append(dict(id=f'm{number:03d}-t{position:03d}-r{repeat:03d}', task=task, method=entry,
                                 label=label, repeat=repeat, compact_limit=settings.get('context_window', 131072)))
    plan = dict(schema='ctxpress.eval.plan', version=1, config=config, jobs=jobs, artifacts=artifacts,
                benchmark=description, code_sha256=eval_plan.fingerprint(),
                author=dict(repository=REPOSITORY, revision=REVISION, sources=sources),
                context_evidence='fresh question; the author agent loop and its own context tools run unchanged',
                run_count=len(jobs), max_parallel=min(config['workers'], len(jobs)),
                agent_timeout_seconds_upper_bound=len(jobs) * settings['timeout'],
                cost_evidence='report actual observed token usage; no total bill estimate',
                missing_environment_files=[] if os.name == 'posix' else ['the author agent runs on Linux'])
    plan['sha256'] = hashlib.sha256(eval_plan.canonical(plan).encode()).hexdigest()
    return plan
