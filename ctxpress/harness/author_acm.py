"""Bounded bridge to ACM's pinned author runner; distinct from Codex ACM tools.

No installation, download, training, server startup, or model call during prepare.
Execution is opt-in and requires an already serving checkpoint and retrieval stack.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
from urllib.parse import urlsplit

from ctxpress.harness import eval_plan
from ctxpress.core import processes
from ctxpress.harness.eval_review import write

REVISION = 'f06f90e728af8580a4515812425c1620144145a2'
REPOSITORY = 'https://github.com/lixiaochuan2020/agentic-context-management'


def regular(path):
    path = Path(path).expanduser().absolute()
    if path.name.lower() == 'auth.json' or not path.is_file() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('expected a regular non-credential input without symlinks')
    return path


def verify_checkout(checkout):
    root = Path(checkout).expanduser().resolve()
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    if git('rev-parse', 'HEAD') != REVISION:
        raise ValueError('ACM checkout must match the supported author revision ' + REVISION)
    if git('status', '--porcelain', '--untracked-files=all', '--', 'src', 'configs', 'main.py', 'pyproject.toml'):
        raise ValueError('ACM source/config checkout has changes; use the pinned author tree')
    files = {}
    for path in sorted((root/'src').rglob('*.py')):
        regular(path)
        files[str(path.relative_to(root))] = eval_plan.file_sha256(path)
    if not all(name in files for name in ('src/run.py', 'src/runner.py', 'src/history.py', 'src/tools.py')):
        raise ValueError('ACM author runner is incomplete')
    return root, files


def prepare(checkout, python, model, api_base, data, config, index, output, limit=1, timeout=1800):
    if type(limit) is not int or limit < 1 or type(timeout) is not int or not 1 <= timeout <= 86400:
        raise ValueError('explicit positive limit and bounded timeout required')
    endpoint = urlsplit(api_base)
    if endpoint.scheme not in ('http', 'https') or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError('API base must be HTTP(S) without inline credentials, query or fragment')
    if not isinstance(model, str) or not model.startswith('openai/') or any(c.isspace() for c in model):
        raise ValueError('use an explicit LiteLLM openai/<served-checkpoint-name> model')
    root, sources = verify_checkout(checkout)
    python, data, config = regular(python), regular(data), regular(config)
    index = Path(index).expanduser().resolve()
    if not index.is_dir():
        raise ValueError('prepared local BM25 index directory required')
    output = Path(output).expanduser().absolute()
    if output.exists() or any(p.is_symlink() for p in (output, *output.parents)) or root == output or root in output.parents:
        raise ValueError('author output must be new and outside its checkout')
    command = [str(python), '-m', 'src.run', '--mode', 'run', '--client', 'litellm',
        '--benchmark', 'browsecomp-plus', '--model', model, '--agent_api_base', api_base,
        '--use_memory_tool', '--data', str(data), '--config', str(config), '--index_path', str(index),
        '--limit', str(limit), '--results_dir', str(output/'results'), '--run_dir', 'ctxpress', '--run_id', 'run']
    document = dict(schema='ctxpress.acm.author_bridge', version=1, repository=REPOSITORY, revision=REVISION,
        cwd=str(root), command=command, timeout=timeout, source_files=sources,
        inputs=[dict(path=str(p), sha256=eval_plan.file_sha256(p)) for p in (python, data, config)],
        index=str(index), model=model, checkpoint_identity_verified=False,
        execution_verified=False, official_grade_verified=False,
        limits=['The serving endpoint must actually host the declared author checkpoint; its model name is not proof.',
                'Dependencies, BM25 corpus/index, summarizer/grader configuration and a served checkpoint must already exist.',
                'This bridge preserves the author runner; it does not add a ninth benchmark family or pool its outcomes with Codex.',
                'An exit code is execution status, not official grade acceptance; index/dependency trees are not fully frozen.'])
    output.mkdir(parents=True, exist_ok=False)
    write(output/'launch.json', document)
    return document


def execute(document, output):
    """Linux process ownership, including cleanup on timeout/cancellation."""
    if not sys.platform.startswith('linux'):
        raise ValueError('author execution currently requires Linux; preparation is portable')
    _, sources = verify_checkout(document['cwd'])
    if sources != document['source_files']:
        raise ValueError('author source changed since preparation')
    for item in document['inputs']:
        if eval_plan.file_sha256(regular(item['path'])) != item['sha256']:
            raise ValueError('author launch input changed')
    output = Path(output)
    if (output/'execution.json').exists() or (output/'author.log').exists():
        raise ValueError('author launch output already used; no implicit retry')
    status = 'failed'
    process = None
    birth = None
    def cancelled(signum, frame):
        raise KeyboardInterrupt('author execution cancelled')
    old = signal.signal(signal.SIGTERM, cancelled)
    try:
        with (output/'author.log').open('xb') as log:
            process = subprocess.Popen(document['command'], cwd=document['cwd'], stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            birth = processes.identity(process.pid)
            try:
                code = process.wait(timeout=document['timeout'])
                status = 'exited' if code == 0 else 'failed'
            except subprocess.TimeoutExpired:
                status = 'timed_out'
    except KeyboardInterrupt:
        status = 'cancelled'
        raise
    finally:
        try:
            if process is not None:
                # Own the fresh process group even if its leader has already exited.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=10)
                write(output/'execution.json', dict(status=status, returncode=process.returncode,
                    pid=process.pid, identity=birth, official_grade_verified=False))
        finally:
            signal.signal(signal.SIGTERM, old)
    return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('checkout', 'python', 'model', 'api-base', 'data', 'config', 'index', 'output'):
        parser.add_argument('--'+flag, required=True)
    parser.add_argument('--limit', type=int, default=1)
    parser.add_argument('--timeout', type=int, default=1800)
    parser.add_argument('--execute', action='store_true', help='explicitly invoke the prepared author runner')
    args = parser.parse_args(argv)
    document = prepare(args.checkout, args.python, args.model, args.api_base, args.data, args.config,
                       args.index, args.output, args.limit, args.timeout)
    print(Path(args.output)/'launch.json')
    if args.execute:
        status = execute(document, args.output)
        print(status)
        return 0 if status == 'exited' else 1
    return 0
