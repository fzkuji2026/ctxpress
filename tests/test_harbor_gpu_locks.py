"""Real Linux file-lock scheduling checks; these do not require or use a GPU."""
import hashlib, json, os, subprocess, sys, tempfile, time, unittest, uuid
from pathlib import Path
from ctxpress.benchmarks.harbor.gpu import reservation

A='GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
B='GPU-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'


@unittest.skipUnless(sys.platform.startswith('linux'),'requires Linux advisory locks')
class ReservationChecks(unittest.TestCase):
    def child(self,ids,daemon,folder,name,hold=False):
        waiting=folder/(name+'-waiting');acquired=folder/(name+'-acquired')
        source='''import time
from pathlib import Path
from ctxpress.benchmarks.harbor.gpu import reservation
def progress(phase):
    if phase == 'waiting': Path(WAITING).write_text('waiting')
with reservation(IDS, DAEMON, progress):
    Path(ACQUIRED).write_text('acquired')
    if HOLD: time.sleep(300)
'''
        values=dict(WAITING=str(waiting),ACQUIRED=str(acquired),IDS=ids,DAEMON=daemon,HOLD=hold)
        prefix='\n'.join(key+'='+repr(value) for key,value in values.items())+'\n'
        process=subprocess.Popen([sys.executable,'-c',prefix+source],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        return process,waiting,acquired

    def wait_for(self,path,process):
        deadline=time.monotonic()+5
        while not path.is_file() and process.poll() is None and time.monotonic()<deadline:time.sleep(0.01)
        self.assertTrue(path.is_file(),process.stderr.read().decode() if process.poll() is not None else 'child did not reach lock state')

    def finish(self,process):
        _,errors=process.communicate(timeout=5)
        self.assertEqual(process.returncode,0,errors.decode())

    def cleanup(self,children,daemons):
        for process in children:
            if process.poll() is None:process.kill()
            process.communicate(timeout=5)
        for daemon in daemons:
            root=Path(tempfile.gettempdir())/('ctxpress-gpu-'+str(os.getuid()))
            key=hashlib.sha256(daemon.encode()).hexdigest()
            for path in root.glob(key+'-*'):path.unlink()

    def test_overlapping_devices_wait_while_disjoint_devices_run(self):
        daemon='fixture-'+uuid.uuid4().hex;children=[]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            try:
                with reservation([A],daemon):
                    first,waiting,acquired=self.child([A],daemon,root,'shared');children.append(first)
                    second,_,other=self.child([B],daemon,root,'disjoint');children.append(second)
                    self.wait_for(waiting,first);self.wait_for(other,second);self.finish(second)
                    self.assertFalse(acquired.exists());self.assertIsNone(first.poll())
                self.wait_for(acquired,first);self.finish(first)
            finally:self.cleanup(children,[daemon])

    def test_worker_crash_releases_kernel_lock(self):
        daemon='fixture-'+uuid.uuid4().hex;children=[]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            try:
                first,_,acquired=self.child([A,B],daemon,root,'crash',hold=True);children.append(first)
                self.wait_for(acquired,first);first.kill();first.communicate(timeout=5)
                second,_,next_acquired=self.child([B,A],daemon,root,'next');children.append(second)
                self.wait_for(next_acquired,second);self.finish(second)
            finally:self.cleanup(children,[daemon])

    def test_different_docker_daemons_do_not_share_device_locks(self):
        first_daemon='fixture-'+uuid.uuid4().hex;second_daemon='fixture-'+uuid.uuid4().hex;children=[]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            try:
                with reservation([A],first_daemon):
                    process,_,acquired=self.child([A],second_daemon,root,'other-daemon');children.append(process)
                    self.wait_for(acquired,process);self.finish(process)
            finally:self.cleanup(children,[first_daemon,second_daemon])


if __name__=='__main__':unittest.main()
