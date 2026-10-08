"""Official Harbor Codex agent hook with a pinned binary and ctxpress wrapper.

The driver supplies read-only runtime/binary mounts and a Unix model channel.
Credentials are uploaded only into the owned container, outside Harbor's log
mounts. The hook preserves official prompt rendering and context extraction.
It does not install an agent, prepare task environments, or change grading.
"""
from __future__ import annotations
import asyncio, copy, json, re, shlex
from pathlib import Path
from ctxpress.harness.runtime.method_inputs import validate_live

PRIVATE = '/ctxpress-private'
HOME = PRIVATE + '/codex'
LOGS = '/logs/agent'


def catalog_input(environment, model, reasoning):
    """Resolve an optional frozen input and check the CLI before any resources."""
    if 'model_catalog' not in environment:
        return {}
    path = environment['model_catalog']
    if not isinstance(path, str) or not path.strip():
        raise ValueError('model_catalog must be an explicit file path')
    from ctxpress.harness.runtime import codex_catalog
    metadata = codex_catalog.inspect(path, model, reasoning)
    path, digest = metadata['path'], metadata['sha256']
    request = dict(model_catalog=path, model_catalog_sha256=digest,
                   model=model, reasoning=reasoning, bindir=environment['bindir'])
    check_catalog(request, probe=True)
    return dict(model_catalog=path, model_catalog_sha256=digest)


def check_catalog(request, *, probe=False):
    """Reject partial declarations, changed files and silent model fallback."""
    fields = {'model_catalog', 'model_catalog_sha256'}
    if not fields.intersection(request):
        return None
    path, digest = request.get('model_catalog'), request.get('model_catalog_sha256')
    if (not isinstance(path, str) or not path.strip() or not Path(path).is_absolute() or
            not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        raise ValueError('catalog request requires an absolute file path and SHA-256')
    from ctxpress.harness.runtime import codex_catalog
    from ctxpress.harness.jobs import plan as eval_plan
    metadata = codex_catalog.inspect(path, request['model'], request['reasoning'])
    if metadata['sha256'] != digest:
        raise ValueError('frozen Codex model catalog changed')
    if probe:
        codex_catalog.preflight(request['bindir'], path, request['model'], request['reasoning'])
        if eval_plan.file_sha256(path) != digest:
            raise ValueError('frozen Codex model catalog changed during preflight')
    return dict(path=path, sha256=digest, model=request['model'], entry_sha256=metadata['entry_sha256'])


def catalog_mounts(request):
    identity = check_catalog(request)
    if identity is None or request.get('pro_replay'):
        return []
    from ctxpress.harness.runtime.codex_catalog import CONTAINER_PATH
    return [dict(type='bind', source=identity['path'], target=CONTAINER_PATH,
                 read_only=True, bind={'create_host_path':False})]


def task_replay(request):
    """Recovery-Bench: commands of the failed attempt (recovery/replay.json in the task), or None."""
    path = Path(request['task']) / 'recovery' / 'replay.json'
    if request.get('pro_replay') or not path.is_file():
        return None
    data = json.loads(path.read_text(encoding='utf-8'))
    commands = data.get('commands')
    if (data.get('schema') != 'ctxpress.recovery_replay' or not isinstance(commands, list) or
            not all(isinstance(c, str) and c for c in commands) or type(data.get('timeout_sec')) is not int):
        raise ValueError('invalid Recovery-Bench replay in the task')
    return dict(commands=commands, timeout_sec=data['timeout_sec'],
                setup_timeout_multiplier=float(data.get('setup_timeout_multiplier') or 1.0))


def replay_settings(request):
    replay = task_replay(request)
    return {'replay':dict(commands=replay['commands'], timeout_sec=replay['timeout_sec'])} if replay else {}


async def replay(environment, replay):
    """recovery_bench.replay.replay_via_exec: each command once, bounded, failures and timeouts ignored."""
    limit = replay['timeout_sec']
    for command in replay['commands']:
        try:
            await asyncio.wait_for(environment.exec(command='bash -lc ' + shlex.quote(command), timeout_sec=limit), timeout=limit)
        except (asyncio.TimeoutError, TimeoutError):
            continue
        except Exception:
            continue


def catalog_settings(request):
    if check_catalog(request) is None or request.get('pro_replay'):
        return {}
    from ctxpress.harness.runtime.codex_catalog import CONTAINER_PATH
    return dict(model_catalog=CONTAINER_PATH, model_catalog_sha256=request['model_catalog_sha256'])


class CallProgress:
    """Incremental official session reader; incomplete JSONL records stay buffered."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.files, self.calls, self.outputs = {}, {}, set()

    def update(self):
        for path in sorted(self.directory.rglob('*.jsonl')):
            offset, remainder = self.files.get(path, (0, b''))
            with path.open('rb') as stream:
                stream.seek(offset); data = stream.read(); offset = stream.tell()
            lines = (remainder + data).split(b'\n')
            self.files[path] = (offset, lines.pop())
            for line in lines:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get('type') != 'response_item':
                    continue
                payload = row.get('payload')
                if not isinstance(payload, dict) or not isinstance(payload.get('call_id'), str):
                    continue
                key = (path, payload['call_id'])
                if payload.get('type') in ('custom_tool_call', 'function_call'):
                    self.calls[key] = True
                elif payload.get('type') in ('custom_tool_call_output', 'function_call_output'):
                    self.outputs.add(key)
        return len(self.calls), bool(self.calls) and self.calls.keys() <= self.outputs


def framework(base, exec_input, settings, limit_error=RuntimeError, credential_state=None):
    settings = copy.deepcopy(settings)
    required = {'method', 'model', 'reasoning', 'binary_version', 'compact_limit', 'upstream'}
    optional = {'auth_file', 'max_calls', 'model_catalog', 'model_catalog_sha256', 'profiles', 'replay'}
    if not isinstance(settings, dict) or set(settings) - required - optional or required - set(settings):
        raise ValueError('declare method, model, reasoning, binary version, compact limit and upstream')
    if {'model_catalog', 'model_catalog_sha256'}.intersection(settings):
        from ctxpress.harness.runtime.codex_catalog import CONTAINER_PATH
        if settings.get('model_catalog') != CONTAINER_PATH:
            raise ValueError('Agent model_catalog must be the read-only container catalog path')
        digest = settings.get('model_catalog_sha256')
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('Agent model_catalog requires its frozen SHA-256')
    for key in ('model', 'reasoning', 'binary_version', 'upstream'):
        if not isinstance(settings[key], str) or not settings[key].strip():
            raise ValueError(key + ' must be explicit')
    if settings['compact_limit'] is not None and (type(settings['compact_limit']) is not int or settings['compact_limit'] <= 0):
        raise ValueError('compact limit must be a positive integer, or None for the host default')
    if 'max_calls' in settings and (type(settings['max_calls']) is not int or settings['max_calls'] <= 0):
        raise ValueError('max_calls must be a positive integer')
    validate_live(settings['method'], settings.get('profiles'))

    class CtxpressCodex(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._ctxpress_proxy = None
            self._ctxpress_home_owned = False
            self.ctxpress_credentials_cleaned = False
            self.ctxpress_stop = None

        def get_version_command(self):
            return '/cxbin/codex --version'

        async def _checked(self, environment, command, **kwargs):
            result = await environment.exec(command=command, timeout_sec=30, **kwargs)
            if result.return_code:
                # Do not embed command output: setup or cleanup diagnostics could
                # include provider secrets from the container environment.
                raise RuntimeError('ctxpress Harbor setup/cleanup command failed')
            return result

        async def setup(self, environment):
            try:
                await self._setup(environment)
            except BaseException:
                cleanup = asyncio.create_task(self.cleanup_credentials(environment))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                raise

        async def _setup(self, environment):
            self.ctxpress_credentials_cleaned = False
            # Probe the runtime inside the actual Agent image before uploading
            # auth or opening the model relay; host readiness alone is weaker.
            readiness = 'from ctxpress.harness.runtime.codex_binary import preflight; preflight("/cxbin")'
            if 'model_catalog' in settings:
                readiness += '\nfrom ctxpress.harness.runtime.codex_catalog import preflight as catalog_preflight\ncatalog = catalog_preflight(' + \
                    ', '.join(repr(value) for value in ('/cxbin', settings['model_catalog'], settings['model'], settings['reasoning'])) + ')\n' + \
                    'if catalog["sha256"] != ' + repr(settings['model_catalog_sha256']) + ':\n' + \
                    '    raise ValueError("mounted Codex model catalog differs from the frozen input")\n'
            await self._checked(environment, 'python3 -c ' + shlex.quote(readiness),
                env={'PYTHONPATH':'/ctxpress-runtime'})
            if settings.get('replay'):
                await replay(environment, settings['replay'])
            await self._checked(environment, 'test ! -e ' + PRIVATE + ' && test ! -L ' + PRIVATE +
                ' && (umask 077; mkdir ' + PRIVATE + ' && mkdir ' + HOME + ')')
            self._ctxpress_home_owned = True
            # No auth material is written in the host attempt directory or logs.
            if settings.get('auth_file'):
                if credential_state:
                    credential_state(True)
                await environment.upload_file(settings['auth_file'], HOME + '/auth.json')
                if getattr(environment, 'default_user', None) is not None:
                    result = await environment.exec(command='chown ' + shlex.quote(str(environment.default_user)) +
                        ' ' + HOME + '/auth.json', user='root', timeout_sec=30)
                    if result.return_code:
                        raise RuntimeError('could not assign private auth to the declared Agent user')
                await self._checked(environment, 'chmod 600 ' + HOME + '/auth.json')
            # Agent session records remain available to official post-run parsing.
            await self._checked(environment, 'mkdir -p ' + LOGS + '/sessions; ln -s ' + LOGS + '/sessions ' + HOME + '/sessions')
            version = await self._checked(environment, self.get_version_command())
            actual = re.fullmatch(r'codex-cli\s+(\S+)', (version.stdout or '').strip())
            if not actual or actual.group(1) != settings['binary_version']:
                raise ValueError('container Codex version differs from the pinned binary')
            self._version = actual.group(1)
            # The driver mounts only the model proxy socket, never a Docker socket.
            source = '''import json, subprocess, time
from pathlib import Path
ready = Path('/ctxpress-private/relay.json')
if ready.exists():
    raise RuntimeError('model relay already exists')
process = subprocess.Popen(['python3', '-m', 'ctxpress.harness.runtime.socket_bridge',
    '--socket', '/ctxpress-channel/model.sock', '--ready-file', str(ready)],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for attempt in range(100):
    if ready.is_file():
        print(ready.read_text())
        break
    if process.poll() is not None:
        raise RuntimeError('model relay startup failed')
    time.sleep(0.1)
else:
    process.terminate()
    raise RuntimeError('model relay did not become ready')
'''
            result = await self._checked(environment, 'python3 -c ' + shlex.quote(source),
                env={'PYTHONPATH':'/ctxpress-runtime'})
            record = json.loads(result.stdout)
            proxy = record.get('url', '')
            if not re.fullmatch(r'http://127\.0\.0\.1:[0-9]+', proxy):
                raise ValueError('invalid model relay address')
            self._ctxpress_proxy = proxy
            # Disable web tools independently of the container network policy.
            from ctxpress.core import toml
            config = dict(model=settings['model'], model_reasoning_effort=settings['reasoning'], web_search='disabled')
            if settings['compact_limit'] is not None:          # None: the host's own default threshold
                config['model_auto_compact_token_limit'] = settings['compact_limit']
            if 'model_catalog' in settings:
                config['model_catalog_json'] = settings['model_catalog']
            data = toml.dumps(config)
            await self._checked(environment, 'python3 -c ' + shlex.quote(
                'from pathlib import Path; Path(' + repr(HOME + '/config.toml') + ').write_text(' + repr(data) + ')'))

        def create_run_agent_commands(self, instruction):
            if self._ctxpress_proxy is None:
                raise RuntimeError('Harbor Codex setup has not completed')
            if self.model_name != settings['model']:
                raise ValueError('official Agent model differs from the declared model')
            # Preserve the official skill and MCP registration helpers. Their
            # configuration uses the private home; it contains no host paths.
            registration = []
            for helper in ('_build_register_skills_command', '_build_register_mcp_servers_command'):
                command = getattr(self, helper)()
                if command:
                    registration.append(exec_input(command=command, env={'CODEX_HOME':HOME}))
            entry = settings['method']
            args = ['python3', '-m', 'ctxpress', 'codex', '--codex-bin', '/cxbin/codex',
                    '--method', entry['class'], '--args', json.dumps(entry.get('args') or {}, ensure_ascii=False),
                    '--upstream', settings['upstream'], '--via', self._ctxpress_proxy,
                    '--log', LOGS + '/ctxpress-requests.jsonl', '--store-dir', LOGS + '/ctxpress-store', '--',
                    'exec', '--dangerously-bypass-approvals-and-sandbox', '--skip-git-repo-check',
                    '--model', settings['model'], '--json', '--enable', 'unified_exec',
                    '-c', 'model_reasoning_effort=' + settings['reasoning'],
                    *(['-c', 'model_auto_compact_token_limit=' + str(settings['compact_limit'])]
                      if settings['compact_limit'] is not None else []), '--', instruction]
            # BaseInstalledAgent uses pipefail. Official stdout/context parsing is
            # retained while the ctxpress proxy separately records actual usage.
            owned = ['python3', '-m', 'ctxpress.harness.runtime.agent_process', '--pid-file', PRIVATE + '/agent.json', '--']
            command = shlex.join(owned + args) + ' 2>&1 </dev/null | tee ' + shlex.quote(LOGS + '/' + self._OUTPUT_FILENAME)
            env = dict(CODEX_HOME=HOME, CTXPRESS_HOME=PRIVATE + '/ctxpress', PYTHONPATH='/ctxpress-runtime',
                       HTTPS_PROXY=self._ctxpress_proxy, HTTP_PROXY=self._ctxpress_proxy, NO_PROXY='localhost,127.0.0.1')
            return registration + [exec_input(command=command, env=env)]

        def create_cleanup_commands(self):
            # Official BaseInstalledAgent treats these as best-effort; credential
            # removal must be checked and is instead performed by run's finally.
            return []

        async def cleanup_credentials(self, environment):
            if not self._ctxpress_home_owned:
                # No auth was uploaded; a pre-existing directory is not ours.
                return
            # Killing the host Docker exec client does not kill its container
            # children. Stop the entire owned agent group before grading.
            await self._checked(environment, 'python3 -m ctxpress.harness.runtime.agent_process --pid-file ' + PRIVATE + '/agent.json --stop',
                env={'PYTHONPATH':'/ctxpress-runtime'})
            await self._checked(environment, 'rm -f ' + HOME + '/auth.json; test ! -e ' + HOME + '/auth.json && test ! -L ' + HOME + '/auth.json')
            self.ctxpress_credentials_cleaned = True
            if credential_state:
                credential_state(False)

        async def run(self, instruction, environment, context):
            execution = None
            try:
                if 'max_calls' not in settings:
                    return await super().run(instruction, environment, context)
                progress = CallProgress(self.logs_dir / 'sessions')
                execution = asyncio.create_task(super().run(instruction, environment, context))
                while not execution.done():
                    await asyncio.wait({execution}, timeout=0.2)
                    count, complete = progress.update()
                    if not execution.done() and count >= settings['max_calls'] and complete:
                        self.ctxpress_stop = 'max_calls'
                        execution.cancel()
                        await asyncio.gather(execution, return_exceptions=True)
                        raise limit_error('ctxpress tool call budget exhausted')
                return await execution
            finally:
                if execution is not None and not execution.done():
                    execution.cancel()
                    await asyncio.gather(execution, return_exceptions=True)
                # If official wait_for cancels execution, complete credential
                # deletion before Harbor proceeds to verifier or container stop.
                cleanup = asyncio.create_task(self.cleanup_credentials(environment))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise

    return CtxpressCodex
