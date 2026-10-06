"""Inspect the selected CLI and optionally exercise it against a loopback fixture.

No credentials are copied or loaded from the user's Codex home. Fixture usage is
synthetic and must never be included in real evaluation totals.
"""
from __future__ import annotations
import hashlib, json, os, shutil, signal, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from ctxpress.hosts.codex import launch
from ctxpress.core.processes import detach_options
from ctxpress.live.rewrite import content_text


def inspect_binary(binary=None):
    binary = binary or shutil.which('codex')
    if not binary:
        raise FileNotFoundError('select an installed Codex CLI; doctor never downloads one')
    binary = str(Path(binary).resolve())
    with tempfile.TemporaryDirectory(prefix='ctxpress-inspect-') as directory:
        env = dict(os.environ, CODEX_HOME=directory)
        version = subprocess.run([binary,'--version'],env=env,capture_output=True,text=True,timeout=15,check=True)
        help_result = subprocess.run([binary,'--help'],env=env,capture_output=True,text=True,timeout=15,check=True)
    capabilities = {flag: flag in help_result.stdout for flag in ('--profile','--no-daemon','--strict-config')}
    return dict(binary=binary, version=version.stdout.strip(), sha256=hashlib.sha256(Path(binary).read_bytes()).hexdigest(),
                capabilities=capabilities, supported=all(capabilities.values()),
                evidence='installed binary help/version; no model call or task-quality evidence')


def offline_check(binary=None, directory=None, timeout=45, scenario="default"):
    """Run the real selected CLI with dummy auth and synthetic SSE on loopback."""
    if scenario not in ("default", "profile", "custom-provider"):
        raise ValueError("unknown offline Codex fixture scenario")
    inspected = inspect_binary(binary)
    if not inspected['supported']:
        raise ValueError('selected CLI lacks the current profile/standalone/config-check capabilities')
    root = Path(directory or Path.cwd() / 'runs').resolve()
    root.mkdir(parents=True,exist_ok=True)
    received, blocked = [], []
    guidance = []
    class API(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_CONNECT(self):
            blocked.append(True)
            self.send_response(403); self.send_header('Content-Length','0'); self.end_headers()
        def do_GET(self):
            raw = json.dumps(dict(models=[],data=[],object='list')).encode()
            self.send_response(200); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)))
            received.append(dict(path=self.path, model=value.get('model'),
                provider_parameters=(self.headers.get('X-Ctxpress-Fixture') == 'offline-marker' and
                    parse_qs(urlsplit(self.path).query).get('ctxpress_fixture') == ['true']), guidance=any(item.get('role')=='developer' and
                content_text(item.get('content'))==guidance[0] for item in value.get('input',[]))))
            item = dict(id='msg_fixture',type='message',status='completed',role='assistant',
                        content=[dict(type='output_text',text='fixture completed',annotations=[])])
            response = dict(id='resp_fixture',object='response',created_at=1,status='completed',model='offline-fixture',output=[item],
                            usage=dict(input_tokens=50,output_tokens=2,total_tokens=52,input_tokens_details=dict(cached_tokens=0)))
            events = [dict(type='response.created',response=dict(response,status='in_progress',output=[])),
                      dict(type='response.output_item.added',output_index=0,item=dict(item,status='in_progress',content=[])),
                      dict(type='response.output_text.delta',item_id='msg_fixture',output_index=0,content_index=0,delta='fixture completed'),
                      dict(type='response.output_item.done',output_index=0,item=item),dict(type='response.completed',response=response)]
            raw = ''.join('event: '+event['type']+'\ndata: '+json.dumps(event)+'\n\n' for event in events).encode()
            self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.send_header('Content-Length',str(len(raw)))
            self.end_headers(); self.wfile.write(raw)
    server = HTTPServer(('127.0.0.1',0),API)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    endpoint = f'http://127.0.0.1:{server.server_port}'
    entry = {'class':'SWEPruner','args':{'url':endpoint+'/prune'}}
    guidance.append(launch.build(entry).instructions)
    # Pass the isolated environment directly to the launcher, not the parent process.
    try:
        with tempfile.TemporaryDirectory(prefix='codex-offline-',dir=root) as home:
            env = dict(os.environ,CODEX_HOME=home,CTXPRESS_HOME=home+'/ctxpress',OPENAI_API_KEY='ctxpress-offline-fixture',CODEX_API_KEY='ctxpress-offline-fixture',
                CTXPRESS_STORE_DIR=home+'/ctxpress/store',CTXPRESS_LOG_PATH=home+'/unused.log',CTXPRESS_METHOD_CONFIG=json.dumps(entry),
                CTXPRESS_MCP='',CTXPRESS_DOCTOR_SCENARIO=scenario,NO_PROXY='localhost,127.0.0.1',no_proxy='localhost,127.0.0.1')
            for key in ('HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY','all_proxy'):
                env[key] = endpoint
            original_files = {}
            if scenario != 'default':
                from ctxpress.core import toml
                fixture = dict(model='offline-fixture', model_reasoning_effort='low',
                               openai_base_url=endpoint, features=dict(enable_request_compression=True))
                if scenario == 'custom-provider':
                    fixture['model_provider'] = 'fixture_gateway'
                    fixture['model_providers'] = dict(fixture_gateway=dict(name='offline fixture gateway',
                        base_url=endpoint, env_key='CTX_OFFLINE_KEY', supports_websockets=False,
                        query_params={'ctxpress_fixture':'true'}, http_headers={'X-Ctxpress-Fixture':'offline-marker'}))
                    env['CTX_OFFLINE_KEY'] = 'ctxpress-offline-fixture'
                user_profile = Path(home) / 'offline-fixture.config.toml'
                user_profile.write_text(toml.dumps(fixture), encoding='utf-8')
                original_files[user_profile] = user_profile.read_bytes()
            config = root / (Path(home).name+'.method.json')
            config.write_text(json.dumps(entry),encoding='utf-8')
            try:
                command = [sys.executable,'-m','ctxpress.hosts.codex.doctor','fixture-launch',str(config),inspected['binary']]
                process = subprocess.Popen(command,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                    cwd=Path(__file__).resolve().parents[3], **detach_options())
                timed_out = False
                try:
                    process.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    if os.name=='nt':
                        subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,check=True)
                    else:
                        os.killpg(process.pid,signal.SIGKILL)
                    process.communicate(timeout=10)
                telemetry = launch.summary(str(next(Path(home).glob('ctxpress/runs/*/requests.jsonl'), Path(home)/'absent')))
                leftover = list(Path(home).glob('ctxpress-*.config.toml'))
                unchanged = all(path.read_bytes() == content for path, content in original_files.items())
                model_loaded = bool(received) and all(row['model'] == 'offline-fixture' for row in received)
                provider_loaded = scenario != 'custom-provider' or (bool(received) and all(row['provider_parameters'] for row in received))
                passed = unchanged and model_loaded and provider_loaded and not timed_out and process.returncode==0 and bool(received) and all(row['guidance'] for row in received) and telemetry['requests']>=1 and not leftover
                result = dict(schema='ctxpress.hosts.codex.offline-check',version=1,test_only=True,installed=inspected,
                    scenario=scenario, user_profile_unchanged=unchanged, model_loaded=model_loaded, provider_parameters_loaded=provider_loaded,
                    passed=passed,returncode=process.returncode,timed_out=timed_out,requests=telemetry['requests'],synthetic_input_tokens=telemetry['api_input_tokens'],
                    synthetic_output_tokens=telemetry['api_output_tokens'],method_guidance_reached_fixture=all(row['guidance'] for row in received) and bool(received),
                    owned_profiles_cleaned=not leftover,blocked_auxiliary_connections=len(blocked),
                    evidence='synthetic loopback mechanism; no real task-quality or real API usage evidence')
            finally:
                config.unlink(missing_ok=True)
    finally:
        server.shutdown(); server.server_close()
    return result


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command',choices=['codex','fixture-launch'])
    ap.add_argument('fixture',nargs='?'); ap.add_argument('fixture_binary',nargs='?')
    ap.add_argument('--codex-bin'); ap.add_argument('--offline-check',action='store_true')
    ap.add_argument('--offline-tools-check', action='store_true', help='exercise CWL and DTOC MCP tools with the real CLI and a loopback fake API')
    ap.add_argument('--model', default='offline-fixture')
    ap.add_argument('--model-catalog', help='frozen model catalog for actual tool-mode checks')
    ap.add_argument('--directory'); ap.add_argument('--output')
    ap.add_argument('--scenario', choices=['default','profile','custom-provider'], default='default',
                    help='isolated fixture configuration for --offline-check')
    args = ap.parse_args(argv)
    if args.command=='fixture-launch':
        entry = json.loads(Path(args.fixture).read_text(encoding='utf-8'))
        scenario = os.environ.get('CTXPRESS_DOCTOR_SCENARIO', 'default')
        selection = ['--model','offline-fixture'] if scenario == 'default' else ['--profile','offline-fixture']
        cmd,result = launch.run(entry,codex_bin=args.fixture_binary,tools=False,
            codex_args=['exec','--strict-config','--skip-git-repo-check','--ephemeral','--ignore-rules','--sandbox','read-only',
                        *selection,'--json','Reply fixture completed without using tools.'],
            upstream=entry['args']['url'].removesuffix('/prune') if scenario == 'default' else None)
        raise SystemExit(result[0])
    if args.offline_check and args.offline_tools_check:
        ap.error('choose one offline check')
    if args.offline_tools_check:
        from ctxpress.hosts.codex.toolcheck import check
        root = Path(args.directory or Path.cwd()/'runs'/'offline-tools').resolve()
        checks = [check(args.codex_bin, root, method, model=args.model, model_catalog=args.model_catalog) for method in ('CWL', 'DTOC')]
        if args.model_catalog:
            checks.append(check(args.codex_bin, root/'cwl-graduated', 'CWL', model=args.model,
                                model_catalog=args.model_catalog, scenario='graduated'))
            checks.extend(check(args.codex_bin, root/(method.lower()+'-resume'), method, timeout=90, model=args.model,
                                model_catalog=args.model_catalog, scenario='resume') for method in ('CWL', 'DTOC'))
            checks.append(check(args.codex_bin, root/'hidden-control', 'CWL', model=args.model,
                                model_catalog=args.model_catalog, scenario='hidden-control'))
        result = dict(schema='ctxpress.hosts.codex.offline-tools-suite', version=1, test_only=True, passed=all(c['passed'] for c in checks), checks=checks)
    else:
        result = offline_check(args.codex_bin,args.directory,scenario=args.scenario) if args.offline_check else inspect_binary(args.codex_bin)
    if args.output:
        from ctxpress.core.artifacts import atomic_json
        atomic_json(args.output,result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if not result.get('passed',result.get('supported',False)):
        raise SystemExit(1)


if __name__=='__main__':
    main()
