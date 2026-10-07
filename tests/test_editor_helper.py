import fcntl
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class EditorLockTests(unittest.TestCase):
    def test_status_queries_share_lock_but_lifecycle_still_excludes(self):
        helper = Path(__file__).resolve().parents[1] / 'deploy/mwsb-editor'
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / 'lock'
            with lock.open('w') as descriptor:
                script = '''import importlib.machinery,importlib.util,sys,subprocess
loader=importlib.machinery.SourceFileLoader('helper',sys.argv[1])
spec=importlib.util.spec_from_loader(loader.name,loader)
module=importlib.util.module_from_spec(spec)
loader.exec_module(module)
original=module.os.open
path=sys.argv[2]
module.os.open=lambda p,*a,**kw: original(path if p=='/run/lock/mwsb-editor.lock' else p,*a,**kw)
module.docker=lambda *a,**kw: subprocess.CompletedProcess(a,0,stdout=b'true\\n')
sys.argv=['helper','status']
raise SystemExit(module.main())
'''
                fcntl.flock(descriptor, fcntl.LOCK_SH)
                result = subprocess.run([sys.executable, '-c', script, str(helper), str(lock)], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                result = subprocess.run([sys.executable, '-c', script, str(helper), str(lock)], capture_output=True)
                self.assertEqual(result.returncode, 1)
                self.assertIn(b'unknown', result.stdout)
