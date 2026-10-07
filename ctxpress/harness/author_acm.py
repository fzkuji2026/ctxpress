"""Read-only preparation of ACM author references for unified integration.

No installation, download, training, server startup, or model call during prepare.
Standalone execution is disabled. Baselines run through the shared method/evaluation interfaces.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

from ctxpress.harness.jobs import plan as eval_plan
from ctxpress.harness.results.review import write

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
        execution_verified=False, official_grade_verified=False, execution_supported=False,
        unified_method={'class': 'ACM'},
        limits=['The serving endpoint must actually host the declared author checkpoint; its model name is not proof.',
                'Dependencies, BM25 corpus/index, summarizer/grader configuration and a served checkpoint must already exist.',
                'This is a reference manifest only. Standalone author execution is disabled; it does not register a benchmark.',
                'An exit code is execution status, not official grade acceptance; index/dependency trees are not fully frozen.'])
    output.mkdir(parents=True, exist_ok=False)
    write(output/'launch.json', document)
    return document


def execute(document, output):
    """Compatibility guard: author processes must not bypass the shared evaluator."""
    raise ValueError('standalone ACM execution is disabled; use the registered ACM method through '
                     'ctxpress codex/claude or ctxpress eval. The trained author policy is not yet integrated.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('checkout', 'python', 'model', 'api-base', 'data', 'config', 'index', 'output'):
        parser.add_argument('--'+flag, required=True)
    parser.add_argument('--limit', type=int, default=1)
    parser.add_argument('--timeout', type=int, default=1800)
    parser.add_argument('--execute', action='store_true', help='retired: standalone execution is disabled; use unified methods/eval')
    args = parser.parse_args(argv)
    if args.execute:
        parser.error('standalone author execution is disabled; use the registered ACM method through ctxpress eval/codex/claude')
    prepare(args.checkout, args.python, args.model, args.api_base, args.data, args.config,
                       args.index, args.output, args.limit, args.timeout)
    print(Path(args.output)/'launch.json')
    return 0
