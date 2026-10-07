import json
import os
from pathlib import Path
import tempfile
import unittest
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
