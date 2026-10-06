"""Explicit model metadata inputs; no user configuration or credential discovery."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from ctxpress.harness import codex_binary, eval_plan

CONTAINER_PATH = '/cxmetadata/models.json'


def _load(path, model, reasoning=None):
    path = Path(path).expanduser().absolute()
    # Reject credential aliases before reading or hashing anything.
    if (path.name.lower() == 'auth.json' or path.resolve().name.lower() == 'auth.json' or
            path.is_symlink() or not path.is_file()):
        raise ValueError('model catalog must be an explicit regular non-credential file')
    raw = path.read_bytes()
    try:
        catalog = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError('invalid model catalog JSON') from None
    models = catalog.get('models') if isinstance(catalog, dict) else None
    if (not isinstance(models, list) or not models or
            any(not isinstance(row, dict) or not isinstance(row.get('slug'), str) or not row['slug'] for row in models)):
        raise ValueError('model catalog requires a nonempty models array with explicit slugs')
    if len({row['slug'] for row in models}) != len(models):
        raise ValueError('model catalog contains duplicate model slugs')
    matches = [row for row in models if row['slug'] == model]
    if len(matches) != 1:
        raise ValueError('model catalog must contain the exact selected model: ' + str(model))
    selected = matches[0]
    instructions = selected.get('base_instructions')
    messages = selected.get('model_messages')
    template = messages.get('instructions_template') if isinstance(messages, dict) else None
    if not any(isinstance(value, str) and value.strip() for value in (instructions, template)):
        raise ValueError('selected model catalog entry has no base instructions or instruction template')
    levels = selected.get('supported_reasoning_levels')
    if reasoning is not None and (not isinstance(levels, list) or
            reasoning not in [row.get('effort') for row in levels if isinstance(row, dict)]):
        raise ValueError('selected model catalog does not support the declared reasoning level')
    identity = dict(path=str(path.resolve()), sha256=hashlib.sha256(raw).hexdigest(), model=model,
                    entry_sha256=hashlib.sha256(eval_plan.canonical(selected).encode()).hexdigest())
    return identity, catalog


def inspect(path, model, reasoning=None):
    """Validate the selected entry without running a CLI; return hashes, never prompts."""
    return _load(path, model, reasoning)[0]


def preflight(bindir, path, model, reasoning=None):
    """Ask the pinned CLI to parse the catalog in an isolated, offline, empty home.

    Normal debug models honors model_catalog_json; --bundled bypasses it. A
    nonempty catalog missing the requested model otherwise silently falls back.
    """
    identity, catalog = _load(path, model, reasoning)
    binary = Path(bindir).resolve() / 'codex'
    with tempfile.TemporaryDirectory(prefix='ctxpress-model-catalog-') as home:
        env = codex_binary._environment(home)
        # No real credentials, user config, or model endpoint. Block incidental
        # HTTP requests as well as declaring an unreachable loopback provider.
        env.update(OPENAI_API_KEY='ctxpress-offline-catalog-check',
                   HTTP_PROXY='http://127.0.0.1:9', HTTPS_PROXY='http://127.0.0.1:9',
                   ALL_PROXY='http://127.0.0.1:9', NO_PROXY='127.0.0.1,localhost')
        command = [str(binary),
                   '-c', 'model_catalog_json=' + json.dumps(identity['path']),
                   '-c', 'model=' + json.dumps(model),
                   '-c', 'model_provider="ctxpress-catalog-check"',
                   '-c', 'model_providers.ctxpress-catalog-check={name="Offline catalog check",'
                         'base_url="http://127.0.0.1:9/v1",env_key="OPENAI_API_KEY",wire_api="responses"}',
                   'debug', 'models']
        try:
            result = subprocess.run(command, cwd=home, env=env, stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('Codex model catalog parser preflight failed') from None
    # Neither captured stream is included in exceptions; stdout contains prompts.
    if result.returncode:
        raise ValueError('Codex rejected the frozen model catalog')
    try:
        parsed = json.loads(result.stdout)
        rows = parsed['models']
        if not isinstance(rows, list) or len(rows) != len(catalog['models']):
            raise ValueError
        indexed = {row['slug']: row for row in rows}
        if len(indexed) != len(rows):
            raise ValueError
        # The CLI may add derived fields, but must preserve every frozen value.
        if any(any(indexed[row['slug']].get(key) != value for key, value in row.items())
               for row in catalog['models']):
            raise ValueError
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise ValueError('Codex parsed model catalog differs from its frozen input') from None
    if inspect(path, model, reasoning) != identity:
        raise ValueError('model catalog changed during preflight')
    return dict(identity, parser_verified=True, model_calls=0)
