"""Real Linux process checks using stdlib only; no task/model/Docker invocation."""
import json, os, socket, socketserver, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest.mock import patch
from ctxpress.harness import agent_process, connect_proxy, eval_plan, socket_bridge
from ctxpress.core import processes


@unittest.skipUnless(sys.platform.startswith('linux'), 'requires Linux process groups')
class ProcessChecks(unittest.TestCase):
    def test_container_exec_cancellation_cleanup_kills_agent_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);record=root/'agent.json';child=root/'child.pid'
            source='import subprocess,time; from pathlib import Path; p=subprocess.Popen(["sleep","300"]); Path('+repr(str(child))+').write_text(str(p.pid)); time.sleep(300)'
            wrapper=subprocess.Popen([sys.executable,'-m','ctxpress.harness.agent_process','--pid-file',str(record),
                '--',sys.executable,'-c',source],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                deadline=time.monotonic()+5
                while not (record.is_file() and child.is_file()) and time.monotonic()<deadline:time.sleep(0.01)
                self.assertTrue(record.is_file() and child.is_file())
                pid=int(child.read_text(encoding='utf-8'));expected=processes.identity(pid)
                self.assertTrue(expected)
                agent_process.stop(record);wrapper.wait(timeout=5)
                self.assertFalse(processes.alive(pid,expected));self.assertFalse(record.exists())
            finally:
                if record.exists():agent_process.stop(record)
                if wrapper.poll() is None:wrapper.kill();wrapper.wait()

    def test_short_successful_commands_do_not_leave_process_records(self):
        with tempfile.TemporaryDirectory() as directory:
            record=Path(directory)/'agent.json'
            self.assertEqual(agent_process.run(record,['/bin/true']),0)
            self.assertFalse(record.exists())

    def test_changed_pid_identity_is_not_signalled(self):
        with tempfile.TemporaryDirectory() as directory:
            record=Path(directory)/'agent.json';record.write_text(json.dumps(dict(pid=os.getpid(),identity='proc:incorrect')), encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'PID changed'):agent_process.stop(record)
            self.assertTrue(record.exists())

    def test_unix_model_channel_with_declared_via_preserves_tls_and_half_close(self):
        class Echo(socketserver.BaseRequestHandler):
            def handle(self):
                while True:
                    value=self.request.recv(65536)
                    if not value:self.request.sendall(b'end');return
                    self.request.sendall(value)
        with tempfile.TemporaryDirectory() as directory:
            upstream=socketserver.ThreadingTCPServer(('127.0.0.1',0),Echo);upstream.daemon_threads=True
            target=f'127.0.0.1:{upstream.server_address[1]}'
            via=connect_proxy.make_server('127.0.0.1',0,[target],idle_timeout=3)
            template=connect_proxy.make_server('127.0.0.1',0,[target],idle_timeout=3,via=f'http://127.0.0.1:{via.server_port}')
            handler=template.RequestHandlerClass;template.server_close()
            path=Path(directory)/'model.sock';unix=socket_bridge.unix_server(path,handler)
            loopback=socket_bridge.loopback_server(path,idle_timeout=3)
            servers=[upstream,via,unix,loopback]
            for server in servers:threading.Thread(target=server.serve_forever,daemon=True).start()
            try:
                with socket.create_connection(loopback.server_address,timeout=5) as client:
                    payload=b'\x16\x03\x01opaque Linux TLS-like bytes\x00'
                    client.sendall(f'CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n'.encode()+payload)
                    header=b''
                    while not header.endswith(b'\r\n\r\n'):header+=client.recv(1)
                    self.assertIn(b' 200 ',header)
                    received=b''
                    while len(received)<len(payload):received+=client.recv(65536)
                    self.assertEqual(received,payload)
                    client.shutdown(socket.SHUT_WR);self.assertEqual(client.recv(3),b'end')
            finally:
                for server in reversed(servers):server.shutdown();server.server_close()

    def test_isolated_worker_does_not_import_ambient_pythonpath_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);official=root/'official';source=official/'harbor/src/harbor';source.mkdir(parents=True)
            ambient=root/'ambient';ambient.mkdir()
            name='ctxpress_fixture_ambient_dependency'
            (ambient/(name+'.py')).write_text('raise RuntimeError("ambient module was imported")\n', encoding='utf-8')
            (source/'__init__.py').write_text('import '+name+'\n', encoding='utf-8')
            package=Path(eval_plan.__file__).resolve().parents[2]
            request=root/'request.json';request.write_text(json.dumps(dict(schema='ctxpress.eval.harbor_trial',version=1,
                package=str(package),official=str(official),runtime=dict(python=str(Path(sys.executable).resolve()),
                version=sys.version,platform=sys.platform,sha256=eval_plan.file_sha256(sys.executable)))), encoding='utf-8')
            result=subprocess.run([sys.executable,'-I','-S','-B',str(package/'ctxpress/harness/harbor_worker.py'),str(request),'--check'],
                env=dict(os.environ,PYTHONPATH=str(ambient)),capture_output=True,text=True,timeout=10)
            self.assertNotEqual(result.returncode,0)
            self.assertIn("No module named '"+name+"'",result.stderr)
            self.assertNotIn('ambient module was imported',result.stderr)

    def test_failed_trial_keeps_channel_bind_until_owned_recovery(self):
        from ctxpress.benchmarks import harbor_driver
        from ctxpress.harness import harbor_worker
        project='ctxp-hb-'+'f'*24
        channel=Path(tempfile.mkdtemp(prefix=project+'-channel-'))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);request=root/'request.json'
            request.write_text(json.dumps(dict(schema='ctxpress.eval.harbor_trial',version=1,folder=str(root),
                project=project,label='fixture-job',images={'main':'sha256:'+'a'*64},target='provider.invalid:443',via=None)), encoding='utf-8')
            async def fail(*args):raise RuntimeError('fixture retained environment')
            def docker(*args):return 'fixture-daemon' if args[0]=='info' else ''
            try:
                with patch.object(harbor_worker,'load',return_value=[]),patch.object(harbor_worker,'run_trial',fail),\
                     patch.object(harbor_driver,'docker',docker),patch.object(tempfile,'mkdtemp',return_value=str(channel)):
                    with self.assertRaisesRegex(RuntimeError,'retained environment'):harbor_worker.main([str(request)])
                self.assertTrue(channel.is_dir() and (channel/'owner.json').is_file())
                journal=next(root.glob('resources-harbor-*.json'))
                self.assertFalse(json.loads(journal.read_text(encoding='utf-8'))['cleaned'])
                with patch.object(harbor_driver,'docker',docker),patch.object(processes,'alive',return_value=False):
                    harbor_driver.recover(journal,'fixture-job')
                self.assertFalse(channel.exists())
            finally:
                if channel.exists():harbor_driver.cleanup_channel(dict(channel=str(channel),project=project,label='fixture-job'))


if __name__=='__main__':unittest.main()
