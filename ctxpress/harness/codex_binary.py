"""Offline readiness of the selected CLI bundle; never use the user's home."""
from __future__ import annotations
import json, os, queue, re, struct, subprocess, sys, tempfile, threading
from pathlib import Path

COMPANION = 'codex-code-mode-host'


def _host_platform():
    return sys.platform


def _binary_format(binary):
    with Path(binary).open('rb') as stream:
        header = stream.read(4)
    if header.startswith(b'\x7fELF'):
        return 'elf'
    if header.startswith(b'#!'):
        return 'shebang'
    if header.startswith(b'MZ'):
        return 'pe'
    if header in (b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xcf\xfa\xed\xfe',
                  b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'):
        return 'mach-o'
    return 'opaque'


def _host_compatible(kind):
    host = _host_platform()
    return (kind == 'elf' and host.startswith('linux') or kind == 'pe' and host == 'win32' or
            kind == 'mach-o' and host == 'darwin' or kind == 'shebang' and host != 'win32')


def requires_companion(version):
    # This release family is verified to route tools through the separate host.
    # Do not infer requirements for older or independently built standalone CLIs.
    return bool(re.fullmatch(r'(?:codex-cli\s+)?0\.159\.\d+(?:[-+][\w.-]+)?', version or ''))


def _environment(home):
    return dict(PATH=os.defpath, HOME=home, CODEX_HOME=home, XDG_CONFIG_HOME=home,
                XDG_DATA_HOME=home, XDG_CACHE_HOME=home)


def version(binary):
    with tempfile.TemporaryDirectory(prefix='ctxpress-binary-version-') as home:
        result = subprocess.run([str(binary), '--version'], cwd=home, env=_environment(home),
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15, check=True)
    match = re.fullmatch(r'codex-cli\s+(\S+)', result.stdout.strip())
    if not match:
        raise ValueError('selected Codex binary returned an invalid version')
    return match.group(1)


def probe_companion(binary, timeout=10):
    """Negotiate IPC v1 and execute text(6 * 7); no model, tools or credentials.

    The probe deliberately uses no delegated tools. It verifies the actual JS
    runtime and IPC used by the 0.159 CLI, beyond an executable/help check.
    """
    responses = queue.Queue()
    with tempfile.TemporaryDirectory(prefix='ctxpress-code-mode-health-') as home:
        process = subprocess.Popen([str(binary), '--listen', 'stdio'], cwd=home, env=_environment(home),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        def read():
            try:
                while True:
                    header = process.stdout.read(4)
                    if len(header) != 4:
                        raise ValueError('code-mode host closed its IPC stream')
                    size = struct.unpack('<I', header)[0]
                    if size > 1024 * 1024:
                        raise ValueError('code-mode health response exceeds frame limit')
                    body = process.stdout.read(size)
                    if len(body) != size:
                        raise ValueError('code-mode host returned a partial frame')
                    responses.put(json.loads(body))
            except Exception as error:
                responses.put(error)
        reader = threading.Thread(target=read, daemon=True); reader.start()
        def receive():
            try:
                value = responses.get(timeout=timeout)
            except queue.Empty:
                raise ValueError('code-mode host health probe timed out') from None
            if isinstance(value, Exception):
                raise ValueError('code-mode host health probe failed') from value
            return value
        def send(value):
            raw = json.dumps(value).encode()
            process.stdin.write(struct.pack('<I', len(raw)) + raw); process.stdin.flush()
        def operation(identity, request):
            send(dict(type='operation/request', id=identity, request=request))
            response = receive()
            if (response.get('type') != 'operation/response' or response.get('id') != identity or
                    response.get('result', {}).get('status') != 'ok'):
                raise ValueError('code-mode host rejected health operation')
            return response['result']['value']
        try:
            send(dict(type='connection/hello', supportedVersions=[1], requiredCapabilities=[], optionalCapabilities=[]))
            hello = receive()
            if hello.get('type') != 'connection/ready' or hello.get('selectedVersion') != 1:
                raise ValueError('code-mode host health handshake failed')
            session = 'ctxpress-health'
            opened = operation(1, dict(method='session/open', sessionId=session))
            if opened != dict(type='session/ready', sessionId=session):
                raise ValueError('code-mode host health session failed')
            started = operation(2, dict(method='session/execute', sessionId=session,
                request=dict(tool_call_id='ctxpress-health', enabled_tools=[], source='text(6 * 7)',
                             yield_time_ms=1000, max_output_tokens=100)))
            response = receive()
            result = response.get('result', {})
            value = result.get('value', {}).get('Result', {})
            if (started.get('type') != 'execution/started' or response.get('type') != 'execute/initialResponse' or
                    response.get('id') != 2 or result.get('status') != 'ok' or value.get('error_text') is not None or
                    value.get('content_items') != [dict(type='input_text', text='42')]):
                raise ValueError('code-mode host did not execute health JavaScript')
            return dict(protocol_version=1, javascript_executed=True, output='42', model_calls=0)
        except (OSError, BrokenPipeError) as error:
            raise ValueError('code-mode host health probe failed') from error
        finally:
            process.kill(); process.wait(timeout=5)
            reader.join(timeout=5)
            process.stdin.close(); process.stdout.close()


def inspect(bindir, *, probe=False, bindings=None):
    binary = Path(bindir) / 'codex'; companion = binary.with_name(COMPANION)
    paths = [binary]; missing = []; detected = None; health = None
    kind = None; unavailable = None
    if not binary.is_file():
        missing.append(str(binary))
    else:
        kind = _binary_format(binary)
        if kind != 'opaque' and not _host_compatible(kind):
            unavailable = 'selected CLI format ' + kind + ' cannot be probed on ' + _host_platform()
        elif kind != 'opaque' and not os.access(binary, os.X_OK):
            missing.append(str(binary) + ': selected CLI is not executable')
        elif kind != 'opaque':
            # Compatible executable failures must propagate, including loader,
            # interpreter and version errors. Never reinterpret them as fixtures.
            detected = version(binary)
    # Planning can hash opaque fixtures or foreign binaries even when Windows
    # reports X_OK for them. Actual preflight cannot certify a foreign runtime.
    required = requires_companion(detected)
    if required or companion.is_file():
        paths.append(companion)
        if not companion.is_file():
            missing.append(str(companion))
        elif required and not os.access(companion, os.X_OK):
            missing.append(str(companion) + ': companion is not executable')
        elif required and bindings is not None and str(companion.resolve()) not in bindings:
            missing.append(str(companion) + ': companion is not bound to the frozen plan')
    if probe and required and not missing:
        health = probe_companion(companion)
    return dict(version=detected, paths=paths, missing=missing, companion_required=required, health=health,
                binary_format=kind, preflight_unavailable=unavailable)


def capture(bindir):
    from ctxpress.harness import eval_plan
    inspected = inspect(bindir)
    return ({str(path): eval_plan.file_sha256(path) for path in inspected['paths'] if path.is_file()},
            inspected['missing'])


def preflight(bindir, *, bindings=None):
    inspected = inspect(bindir, probe=True, bindings=bindings)
    if inspected['missing']:
        raise ValueError('Codex binary bundle is incomplete: ' + '; '.join(inspected['missing']))
    if inspected['preflight_unavailable']:
        raise ValueError('Codex binary preflight unavailable: ' + inspected['preflight_unavailable'])
    return inspected
