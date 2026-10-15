"""One proxy for all host launches of one agent run.

A benchmark harness may restart Codex several times in one run (resuming the same session after each
milestone). A proxy per launch would start every resume with an empty method: no summaries, no masking
state, and the host's whole accumulated history forwarded at once. The shared proxy keeps the method's
live session in one process; each launch attaches its own request log and receipt store, then detaches.

    port = attach(directory, config)    # starts the proxy on first use, reuses it afterwards
    ...run the host against 127.0.0.1:port...
    detach(port, config)
"""
from __future__ import annotations
import argparse, fcntl, hashlib, json, os, subprocess, sys, time, urllib.request
from pathlib import Path
from ctxpress.core import processes

STATE = 'proxy.json'


def key_of(config):
    """The configuration a launch may share: method, upstream route and tool mode, not its own log/store."""
    shared = {k: config.get(k) for k in ('entry', 'upstream', 'via', 'tools')}
    return hashlib.sha256(json.dumps(shared, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def call(port, path, payload, timeout=90):
    request = urllib.request.Request(f'http://127.0.0.1:{port}/__ctxpress/{path}', data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read())


def _live(directory, key):
    path = directory / STATE
    if not path.exists():
        return None
    state = json.loads(path.read_text(encoding='utf-8'))
    if not processes.alive(state['pid'], state['identity']):
        return None
    if state['key'] != key:
        raise RuntimeError('a shared proxy with a different method configuration is running')
    try:
        if call(state['port'], 'health', dict(key=key), timeout=10).get('key') != key:
            raise RuntimeError('shared proxy answered for a different configuration')
    except OSError:
        return None
    return state['port']


def attach(directory, config, timeout=60):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = key_of(config)
    with open(directory / 'lock', 'a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        port = _live(directory, key)
        if port is None:
            started = directory / ('config-' + key[:16] + '.json')
            started.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
            (directory / STATE).unlink(missing_ok=True)
            with open(directory / 'proxy.log', 'ab') as output:
                child = subprocess.Popen([sys.executable, '-m', 'ctxpress.hosts.codex.shared_proxy', '--config', str(started),
                                          '--state', str(directory / STATE)], stdin=subprocess.DEVNULL, stdout=output,
                                         stderr=subprocess.STDOUT, env=dict(os.environ), **processes.detach_options())
            deadline = time.time() + timeout
            while port is None:
                if child.poll() is not None:
                    raise RuntimeError('shared proxy exited during start; see ' + str(directory / 'proxy.log'))
                if time.time() > deadline:
                    raise RuntimeError('shared proxy did not start in time')
                time.sleep(0.2)
                port = _live(directory, key)
    call(port, 'attach', dict(key=key, log=config['log'], store_dir=config['store_dir']))
    return port


def detach(port, config):
    return call(port, 'detach', dict(key=key_of(config), log=config['log']))


def main(argv=None):
    from ctxpress.live.factory import frozen_factory
    from ctxpress.live.proxy import serve
    from ctxpress.live.control_receipts import ControlReceipts
    from ctxpress.methods import build
    from ctxpress.core.artifacts import atomic_json
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', required=True)
    ap.add_argument('--state', required=True)
    a = ap.parse_args(argv)
    config = json.loads(Path(a.config).read_text(encoding='utf-8'))
    sample = build(config['entry'])
    receipts = ControlReceipts(config['store_dir'], config['log']) if config['tools'] and sample.agent_tools else None
    srv, _ = serve(frozen_factory(sample), 0, config['upstream'], config.get('via'), config['log'], host='127.0.0.1',
                   store_dir=config['store_dir'], store_prefix=config['store_dir'], retrieve_tool=config['tools'],
                   codex_method_tools=config['tools'], control_receipts=receipts, shared_key=key_of(config))
    atomic_json(a.state, dict(pid=os.getpid(), identity=processes.identity(os.getpid()),
                              port=srv.server_address[1], key=key_of(config)))
    srv.serve_forever()


if __name__ == '__main__':
    main()
