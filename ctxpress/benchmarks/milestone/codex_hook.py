"""Process-local official Codex hook; it changes agent launch, not benchmark orchestration.

The trial driver must prepare and freeze these inputs before installing the hook.
No package installers or downloads are introduced by the agent initialization.
"""
from __future__ import annotations
import contextlib, copy, json, re, shlex, uuid
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from ctxpress.benchmarks.method_inputs import validate_live


def check_catalog(execution, *, probe=False):
    fields={'model_catalog','model_catalog_sha256'}
    present=fields.intersection(execution)
    if not present:return None
    if present!=fields:raise ValueError('native model catalog path and SHA-256 must be declared together')
    path,digest=execution['model_catalog'],execution['model_catalog_sha256']
    if not isinstance(path,str) or not Path(path).is_absolute() or ':' in path:
        raise ValueError('invalid native model_catalog file path')
    if not isinstance(digest,str) or not re.fullmatch(r'[0-9a-f]{64}',digest):
        raise ValueError('invalid native model_catalog SHA-256')
    from ctxpress.harness.runtime import codex_catalog
    metadata=codex_catalog.inspect(path,execution['model'],execution['reasoning'])
    if metadata['sha256']!=digest:raise ValueError('native model catalog differs from bound SHA-256')
    if probe:
        parsed=codex_catalog.preflight(execution['bindir'],path,execution['model'],execution['reasoning'])
        if parsed['sha256']!=digest or codex_catalog.inspect(path,execution['model'],execution['reasoning'])!=metadata:
            raise ValueError('native model catalog changed during preflight')
    return metadata


def catalog_input(environment, model, reasoning):
    if 'model_catalog' not in environment:return {}
    path=environment['model_catalog']
    if not isinstance(path,str) or not Path(path).is_absolute() or ':' in path:
        raise ValueError('invalid native model_catalog file path')
    from ctxpress.harness.runtime import codex_catalog
    metadata=codex_catalog.inspect(path,model,reasoning)
    binding=dict(model_catalog=metadata['path'],model_catalog_sha256=metadata['sha256'])
    check_catalog(dict(binding,model=model,reasoning=reasoning,bindir=environment['bindir']),probe=True)
    return binding


def framework(base, settings):
    settings = copy.deepcopy(settings)
    required = {'runtime', 'bindir', 'logs', 'store', 'method', 'binary_version', 'compact_limit'}
    if required - set(settings) or set(settings) - required - {'auth_file','upstream','via','private_runtime','channel','profiles','model_catalog'}:
        raise ValueError('official Codex hook requires explicit runtime, binary, log/store paths, method and version')
    keys=['runtime','bindir','logs','store']+[key for key in ('channel','profiles','model_catalog') if key in settings]
    for key in keys:
        if not isinstance(settings[key], str) or not PurePosixPath(settings[key]).is_absolute() or ':' in settings[key]:
            raise ValueError(f'{key} must be an absolute Linux mount path')
    if 'model_catalog' in settings:
        from ctxpress.harness.runtime.codex_catalog import CONTAINER_PATH
        catalog_target=CONTAINER_PATH
    if type(settings['compact_limit']) is not int or settings['compact_limit'] <= 0:
        raise ValueError('compact_limit must be a positive integer')
    if not isinstance(settings['binary_version'], str) or not settings['binary_version']:
        raise ValueError('binary version must be explicit')
    validate_live(settings['method'], settings.get('profiles'))
    private=settings.get('private_runtime',False)
    if type(private) is not bool:raise ValueError('private_runtime must be boolean')
    if private:
        if settings.get('auth_file') or settings.get('via') or not settings.get('channel'):
            raise ValueError('private native runtime requires an owned channel and separate credential upload')
        if not isinstance(settings.get('upstream'),str):raise ValueError('private native model upstream must be explicit')
        parsed=urlsplit(settings['upstream'])
        if parsed.scheme!='https' or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError('private native model upstream requires HTTPS without embedded credentials')
    elif settings.get('channel'):raise ValueError('a native model channel requires private_runtime')

    class CtxpressCodex(base):
        ctxpress_private_runtime=private
        _model_relay=None
        def __init__(self, *args, **kwargs):
            # The author selector validates versions for its npm installer.
            # Our existing binary can have a prerelease version; it is checked
            # exactly by get_requested_version/version_matches_request below.
            # Keep that installer selector out of the base constructor.
            requested=kwargs.pop('agent_version',None)
            if requested is not None and requested!=settings['binary_version']:
                raise ValueError('native requested Codex version differs from the frozen binary')
            super().__init__(*args, **kwargs)
            if settings.get('auth_file'):
                self._codex_auth_file = Path(settings['auth_file'])
            if private:
                self.api_key=None;self.base_url=None
                self._codex_auth_file=Path('/home/fakeroot/.codex/auth.json')
                self._codex_config_file=Path('/home/fakeroot/.codex/config.toml')

        @classmethod
        def set_model_relay(cls,url):
            if not private or not re.fullmatch(r'http://127\.0\.0\.1:[0-9]+',url) or not 0<int(url.rsplit(':',1)[1])<65536:
                raise ValueError('native model relay must be an owned loopback address')
            if cls._model_relay is not None and cls._model_relay!=url:
                raise ValueError('native persistent Agent cannot switch its model relay')
            cls._model_relay=url

        def get_container_mounts(self):
            if private:
                # Package only, with sessions mounted as an actual directory so
                # the author's find-based recovery/stat parsers keep working.
                mounts=['-v',settings['runtime']+'/ctxpress:/ctxpress-runtime/ctxpress:ro',
                    '-v',settings['logs']+'/sessions:/home/fakeroot/.codex/sessions:rw',
                    '-v',settings['channel']+':/ctxpress-channel:ro']
            else:mounts=list(super().get_container_mounts())
            for key, target, mode in (('runtime', '/ctxpress-runtime', 'ro'), ('bindir', '/cxbin', 'ro'),
                                      ('logs', '/ctxpress-logs', 'rw'), ('store', '/ctxpress-store', 'rw')):
                if private and key=='runtime':continue
                mounts += ['-v', f"{settings[key]}:{target}:{mode}"]
            if settings.get('profiles'):mounts+=['-v',settings['profiles']+':/ctxpress-method:ro']
            if 'model_catalog' in settings:mounts+=['-v',settings['model_catalog']+':'+catalog_target+':ro']
            return mounts

        def get_container_env_vars(self):
            return [] if private else super().get_container_env_vars()

        def get_requested_version(self):
            return settings['binary_version']

        def get_version_command(self):
            return ['/cxbin/codex', '--version']

        def parse_version_output(self, output):
            match = re.fullmatch(r'codex-cli\s+(\S+)', (output or '').strip())
            return match.group(1) if match else None

        def version_matches_request(self, actual_version):
            return actual_version == settings['binary_version']

        def get_container_init_script(self, agent_name):
            # ContainerSetup executes this as Python, not as a shell script.
            if private:
                return '''from pathlib import Path
import os,pwd
binary=Path('/cxbin/codex')
if not binary.is_file() or not os.access(binary,os.X_OK):
    raise RuntimeError('declared Codex binary is unavailable')
if not Path('/ctxpress-runtime/ctxpress/__main__.py').is_file():
    raise RuntimeError('frozen ctxpress runtime is unavailable')
user=pwd.getpwnam('fakeroot')
home=Path('/home/fakeroot/.codex')
if home.is_symlink():
    raise RuntimeError('private native home cannot be a symbolic link')
if (home/'auth.json').exists() or (home/'auth.json').is_symlink():
    raise RuntimeError('prepared image contains pre-existing Agent authentication')
if (home/'config.toml').exists() or (home/'config.toml').is_symlink():
    raise RuntimeError('prepared image contains pre-existing Agent configuration')
home.mkdir(mode=0o700,exist_ok=True)
os.chmod(home,0o700);os.chown(home,user.pw_uid,user.pw_gid)
private=Path('/ctxpress-private')
if private.exists() or private.is_symlink():
    raise RuntimeError('private native process storage already exists')
private.mkdir(mode=0o700);os.chown(private,user.pw_uid,user.pw_gid)
'''
            return '''from pathlib import Path
import os, shutil
binary = Path('/cxbin/codex')
if not binary.is_file() or not os.access(binary, os.X_OK):
    raise RuntimeError('declared Codex binary is unavailable')
if not Path('/ctxpress-runtime/ctxpress/__main__.py').is_file():
    raise RuntimeError('frozen ctxpress runtime is unavailable')
home = Path.home() / '.codex'
home.mkdir(mode=0o700, exist_ok=True)
source = Path('/tmp/host-codex/auth.json')
if source.is_file():
    destination = home / 'auth.json'
    shutil.copyfile(source, destination)
    destination.chmod(0o600)
'''

        def _wrap(self, command):
            if not command.startswith('codex '):
                raise ValueError('official Codex command shape changed; refusing to bypass ctxpress')
            entry = settings['method']
            environment=['env','PYTHONPATH=/ctxpress-runtime']
            if private:
                if self._model_relay is None:raise ValueError('native model relay has not been prepared')
                environment+=['CODEX_HOME=/home/fakeroot/.codex','CTXPRESS_HOME=/ctxpress-private/ctxpress',
                    'HTTPS_PROXY='+self._model_relay,'HTTP_PROXY='+self._model_relay,'NO_PROXY=localhost,127.0.0.1',
                    'python3','-m','ctxpress.harness.runtime.agent_process','--pid-file','/ctxpress-private/agent.json','--']
            args = environment+['python3', '-m', 'ctxpress', 'codex',
                    '--codex-bin', '/cxbin/codex', '--method', entry['class'],
                    '--args', json.dumps(entry.get('args') or {}, ensure_ascii=False),
                    '--log', '/ctxpress-logs/' + uuid.uuid4().hex + '.jsonl', '--store-dir', '/ctxpress-store']
            for key in ('upstream', 'via'):
                if settings.get(key):
                    args += ['--' + key, settings[key]]
            if private:args+=['--via',self._model_relay]
            overrides=['--', '-c', f"model_auto_compact_token_limit={settings['compact_limit']}"]
            if 'model_catalog' in settings:overrides+=['-c','model_catalog_json='+json.dumps(catalog_target)]
            return shlex.join(args + overrides) + ' ' + command[len('codex '):]

        def build_run_command(self, model, session_id, prompt_path):
            return self._wrap(super().build_run_command(model, session_id, prompt_path))

        def build_resume_command(self, model, session_id, message_path):
            return self._wrap(super().build_resume_command(model, session_id, message_path))

    return CtxpressCodex


@contextlib.contextmanager
def installed(settings):
    from harness.e2e.agents import get_agent_framework
    from harness.e2e.agents.base import _FRAMEWORK_REGISTRY
    # Load official implementations before replacing the codex entry so lazy
    # registration cannot overwrite the hook on the first actual launch.
    get_agent_framework('codex')
    original = _FRAMEWORK_REGISTRY['codex']
    replacement = framework(original, settings)
    _FRAMEWORK_REGISTRY['codex'] = replacement
    try:
        yield replacement
    finally:
        _FRAMEWORK_REGISTRY['codex'] = original
