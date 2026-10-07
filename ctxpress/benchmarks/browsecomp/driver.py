"""Run one BrowseComp-Plus question with the pinned ACM author agent behind ctxpress proxies.

Three local proxies carry every model call of a job, each to its own log:
  agent       the agent model (the served checkpoint); the planned method rewrites its Chat Completions turns;
  summarizer  the author's manage_context / query_memory summarizer, forwarded unchanged;
  grader      the author's LLM judge, forwarded unchanged and billed apart from the method.
The author process gets the proxies as its API bases and a placeholder key; each proxy adds its upstream's real
credential from the job environment (CTXPRESS_AGENT_API_KEY, CTXPRESS_SUMMARIZER_API_KEY, CTXPRESS_GRADER_API_KEY),
so the author code never holds one and neither the plan, the logs nor the result contain them. The process runs in its own session and is killed as a group on timeout or cancellation.
"""
from __future__ import annotations
import json, os, signal, subprocess, threading, time
from pathlib import Path

from ctxpress.core import artifacts as artifact_io
from ctxpress.core import processes

PLACEHOLDER = 'ctxpress-proxy-holds-the-key'
GRADE = r"""
import json, sys
from src.evaluator import evaluate_browsecomp_plus
spec = json.load(open(sys.argv[1], encoding='utf-8'))
evaluate_browsecomp_plus(input_dir=spec['input_dir'], ground_truth=spec['ground_truth'], eval_dir=spec['eval_dir'],
                         model=spec['model'], qrel_evidence_path=spec.get('qrels'))
"""


# The author agent appends this to its newest tool result and drops it from the previous one each turn.
TOKEN_HINT = r"\n\n\[CURRENT CONTEXT TOKEN: \d+\]"


def start_proxy(entry, upstream, log, store_dir=None, key=None, session=None):
    from ctxpress.live.proxy import serve
    from ctxpress.live.factory import frozen_factory
    from ctxpress.methods import build
    server, _ = serve(frozen_factory(build(entry)), 0, upstream, log=str(log), host='127.0.0.1', store_dir=store_dir,
                      upstream_key=os.environ.get(key) if key else None,
                      chat_session=session, chat_volatile=TOKEN_HINT if session else None)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    return server, worker


def stop(servers):
    for server, worker in servers:
        server.shutdown(); server.server_close(); worker.join(timeout=5)


def rows(path):
    found = []
    if Path(path).is_file():
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                found.append(row)
    return found


def run_owned(command, *, cwd, env, log, timeout, journal):
    """Run a child in its own process group; record it so recovery can tell whether it is still alive."""
    with Path(log).open('ab') as output:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        artifact_io.atomic_json(journal, dict(schema='ctxpress.browsecomp.process', version=1, pid=process.pid,
                                              identity=processes.identity(process.pid), finished=False))
        status = 'failed'
        try:
            code = process.wait(timeout=timeout)
            status = 'exited' if code == 0 else 'failed'
        except subprocess.TimeoutExpired:
            status = 'timed_out'
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)        # the group may hold retrieval or tokenizer workers
            except (ProcessLookupError, PermissionError):
                pass
            process.wait(timeout=10)
            artifact_io.atomic_json(journal, dict(schema='ctxpress.browsecomp.process', version=1, pid=process.pid,
                                                  finished=True, status=status, returncode=process.returncode))
    return status


def child_env(extra):
    keep = ('PATH', 'HOME', 'LANG', 'LC_ALL', 'JAVA_HOME', 'JVM_PATH', 'HF_HOME', 'HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE',
            'CUDA_VISIBLE_DEVICES', 'TMPDIR')
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.update(PYTHONNOUSERSITE='1', **extra)
    return env


def execute(adapter, task, entry, config, job, *, folder, label):
    if os.name != 'posix':
        raise ValueError('the ACM author agent runs on Linux')
    environment, settings = config['environment'], config['run']
    folder = Path(folder).resolve(); folder.mkdir(parents=True, exist_ok=True)
    question = adapter.question(task)
    data = folder / 'question.json'
    artifact_io.atomic_json(data, [question])
    logs = dict(agent=folder / 'ctxpress-requests.jsonl', summarizer=folder / 'summarizer-requests.jsonl',
                grader=folder / 'grader-requests.jsonl')
    servers = [start_proxy(entry, environment['agent_upstream'], logs['agent'], store_dir=str(folder / 'store'),
                           key='CTXPRESS_AGENT_API_KEY', session='browsecomp:' + job['id']),
               start_proxy({'class': 'NoCompaction'}, environment['summarizer']['upstream'], logs['summarizer'],
                           key='CTXPRESS_SUMMARIZER_API_KEY')]
    # The upstream URL carries the API prefix (e.g. /v1); clients append only the endpoint path.
    base = lambda server: f'http://127.0.0.1:{server.server_port}'
    agent_base, summarizer_base = base(servers[0][0]), base(servers[1][0])
    results = folder / 'author-results'
    command = [environment['python'], '-m', 'src.run', '--mode', 'run', '--client', 'litellm', '--benchmark', 'browsecomp-plus',
               '--model', config['model'], '--agent_api_base', agent_base, '--summarizer_model', environment['summarizer']['model'],
               '--summarizer_api_base', summarizer_base, '--index_path', environment['index'], '--data', str(data),
               '--results_dir', str(results), '--run_dir', 'ctxpress', '--run_id', job['id'], '--config', '', '--no_skip']
    if settings['use_memory_tool']:
        command.append('--use_memory_tool')
    overrides = [f'{name}={settings[key]}' for key, name in (('retrieval_k', 'retrieval.k'),
                 ('context_window', 'agent.context_window'), ('max_new_tokens', 'agent.rollout.max_new_tokens')) if key in settings]
    if overrides:
        command += ['--override', *overrides]
    keys = dict(OPENAI_API_KEY=PLACEHOLDER)
    artifact_io.atomic_json(folder / 'author-command.json', dict(command=command, cwd=environment['checkout']))
    started = time.monotonic()
    try:
        status = run_owned(command, cwd=environment['checkout'], env=child_env(keys), log=folder / 'author.log',
                           timeout=settings['timeout'], journal=folder / 'process-agent.json')
    finally:
        stop(servers)
    seconds = round(time.monotonic() - started, 1)
    output = adapter.author_result(results, config['model'], job['id'], question['id'])
    grade = None
    if settings['grade']:
        grade = grade_question(adapter, task, config, folder, results, config['model'], job['id'], output)
    from ctxpress.live.telemetry import summary
    requests = rows(logs['agent']) + [dict(row, host_role='summarizer') for row in rows(logs['summarizer'])]
    telemetry = summary(str(logs['agent']))
    result = dict(task=task, benchmark=adapter.describe(), method=entry, model=config['model'], reasoning=config['reasoning'],
                  stop=status if output is None else output.get('status', status),
                  calls=None if output is None else output.get('num_turns'), seconds=seconds,
                  requests=telemetry['requests'], usage=telemetry, rewrites=requests, grade=grade,
                  proxy_log=str(logs['agent']), grading_log=str(logs['grader']), author_status=status,
                  author_result=None if output is None else {key: output.get(key) for key in (
                      'status', 'num_turns', 'tool_call_counts', 'mem_operations', 'usage', 'elapsed_sec', 'error')},
                  use_memory_tool=settings['use_memory_tool'], real_run_verified=False)
    from ctxpress.harness.runtime import execution_health
    return execution_health.retain(result)


def grade_question(adapter, task, config, folder, results, model, run_id, output):
    """The author's LLM judge on this question's result file, through a logging proxy."""
    environment = config['environment']
    if output is None or output.get('status') != 'complete':
        return dict(resolved=False, infra_invalid=output is None, failure_kind=None if output is not None else 'no_author_result',
                    quality_kind='boolean', judge=None, author_status=None if output is None else output.get('status'))
    server = start_proxy({'class': 'NoCompaction'}, environment['grader']['upstream'], folder / 'grader-requests.jsonl',
                         key='CTXPRESS_GRADER_API_KEY')
    spec = folder / 'grade-input.json'
    artifact_io.atomic_json(spec, dict(input_dir=str(adapter.result_dir(results, model, run_id)),
                                       ground_truth=environment['ground_truth'], eval_dir=str(folder / 'author-eval'),
                                       model=environment['grader']['model'], qrels=environment.get('qrels')))
    keys = dict(OPENAI_API_KEY=PLACEHOLDER,
                OPENAI_BASE_URL=f'http://127.0.0.1:{server[0].server_port}')
    try:
        status = run_owned([environment['python'], '-c', GRADE, str(spec)], cwd=environment['checkout'], env=child_env(keys),
                           log=folder / 'grader.log', timeout=1800, journal=folder / 'process-grader.json')
    finally:
        stop([server])
    return adapter.read_grade(task, folder / 'author-eval', grading_status=status)


def recover(path, label):
    """Jobs own no containers; a stale process record only blocks recovery while its process is alive."""
    path = Path(path)
    for journal in path.glob('process-*.json'):
        record = json.loads(journal.read_text(encoding='utf-8'))
        if not record.get('finished') and type(record.get('pid')) is int and processes.alive(record['pid'], record.get('identity')):
            raise ValueError('BrowseComp-Plus author process is still alive; recovery cannot interrupt it')
