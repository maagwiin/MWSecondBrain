import json
import os
from pathlib import Path
import tempfile
import unittest
import time
from unittest.mock import patch

from mwsecondbrain.sync import sync


class SyncAdapterTests(unittest.TestCase):
    def test_no_script_is_not_reported_as_success(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(sync(Path('/missing'))['state'], 'NOT_CONFIGURED')

    def test_immutable_external_script_returns_structured_result(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            vault = base / 'vault'
            vault.mkdir()
            script = base / 'brain_sync.py'
            result = {'state':'SYNCED','message':'ok','pending':False,'head':'a'*40,'remote_checked_now':True}
            script.write_text('print(' + repr(json.dumps(result)) + ')')
            with patch.dict(os.environ, MWSB_SYNC_SCRIPT=str(script)):
                self.assertEqual(sync(vault), result)

    def test_vault_controlled_script_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            script = vault / 'brain_sync.py'
            script.write_text('raise RuntimeError("must never execute")')
            with patch.dict(os.environ, MWSB_SYNC_SCRIPT=str(script)):
                self.assertEqual(sync(vault)['state'], 'NOT_CONFIGURED')

    def test_subprocess_diagnostics_are_not_exposed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vault = root / 'vault'
            vault.mkdir()
            script = root / 'script.py'
            script.write_text('print("secret private value")')
            with patch.dict(os.environ, MWSB_SYNC_SCRIPT=str(script)):
                result = sync(vault)
                self.assertEqual(result['state'], 'ERROR')
                self.assertNotIn('secret', str(result))

    def test_timeout_stops_descendants_before_releasing_vault(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vault = root / 'vault'
            vault.mkdir()
            marker = vault / 'late-write'
            child = 'import time,pathlib; time.sleep(.5); pathlib.Path(' + repr(str(marker)) + ').write_text("late")'
            script = root / 'script.py'
            script.write_text('import subprocess,sys,time\nsubprocess.Popen([sys.executable,"-c",' + repr(child) + '])\ntime.sleep(5)\n')
            with patch.dict(os.environ, MWSB_SYNC_SCRIPT=str(script)), patch('mwsecondbrain.sync.TIMEOUT_SECONDS', .1, create=True):
                self.assertEqual(sync(vault)['state'], 'ERROR')
            time.sleep(.7)
            self.assertFalse(marker.exists(), 'A writer survived the controller operation')
