import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from mwsecondbrain.backup import backup, restore, BackupError


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / 'vault'
        self.state = self.root / 'state'
        self.archives = self.root / 'archives'
        self.vault.mkdir()
        self.state.mkdir()

    def put(self, root, name, content='content'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def test_live_wal_snapshot_and_full_restore(self):
        for name in ['note.md', '.git/objects/object', '.obsidian/workspace.json', 'assets/picture.png']:
            self.put(self.vault, name)
        self.put(self.state, 'attachments/document.pdf')
        config = self.root / 'config'
        self.put(config, 'preferences.json')
        connection = sqlite3.connect(self.state / 'state.sqlite3')
        self.addCleanup(connection.close)
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('CREATE TABLE notes (text TEXT)')
        connection.execute("INSERT INTO notes VALUES ('committed WAL data')")
        connection.commit()
        self.assertTrue((self.state / 'state.sqlite3-wal').exists())
        with patch.dict(os.environ, {'MWSB_OBSIDIAN_CONFIG_DIR': str(config)}):
            result = backup(self.vault, self.state, self.archives)
        self.assertEqual(result['state'], 'success')
        destination = self.root / 'restored'
        restore(Path(result['archive']), destination)
        for name in ['note.md', '.git/objects/object', '.obsidian/workspace.json', 'assets/picture.png']:
            self.assertEqual((destination / 'vault' / name).read_text(), 'content')
        self.assertTrue((destination / 'state/attachments/document.pdf').exists())
        self.assertTrue((destination / 'obsidian/preferences.json').exists())
        with sqlite3.connect(destination / 'state/state.sqlite3') as restored:
            self.assertEqual(restored.execute('SELECT text FROM notes').fetchone()[0], 'committed WAL data')
        self.assertFalse((destination / 'state/state.sqlite3-wal').exists())

    def test_secrets_excluded_and_symlink_rejected(self):
        for name in ['.env', '.env.production', 'auth.json', 'credentials/token', 'private.key', 'cert.pem', '.aws/secret']:
            self.put(self.vault, name, 'secret')
        self.put(self.vault, 'safe.md')
        result = backup(self.vault, self.state, self.archives)
        with tarfile.open(result['archive']) as archive:
            self.assertEqual(set(archive.getnames()), {'vault/safe.md', 'manifest.json'})
        (self.vault / 'linked').symlink_to(self.root / 'outside')
        before = set(self.archives.iterdir())
        with self.assertRaises(BackupError):
            backup(self.vault, self.state, self.archives)
        self.assertEqual(set(self.archives.iterdir()), before)

    def malicious(self, name, kind=None, manifest=None):
        path = self.root / 'malicious.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            entry = tarfile.TarInfo(name)
            if kind:
                entry.type = kind
                entry.linkname = '/etc/passwd'
                archive.addfile(entry)
            else:
                entry.size = 4
                archive.addfile(entry, io.BytesIO(b'data'))
            payload = json.dumps(manifest or {'version': 1, 'files': {name: 'wrong'}}).encode()
            entry = tarfile.TarInfo('manifest.json')
            entry.size = len(payload)
            archive.addfile(entry, io.BytesIO(payload))
        return path

    def test_restore_rejects_unsafe_entries_and_checksum(self):
        for name, kind in [('../escape', None), ('/absolute', None), ('vault/link', tarfile.SYMTYPE), ('vault/link', tarfile.LNKTYPE), ('vault/device', tarfile.CHRTYPE), ('vault/file', None)]:
            with self.subTest(name=name, kind=kind):
                destination = self.root / 'restored'
                with self.assertRaises(BackupError):
                    restore(self.malicious(name, kind), destination)
                self.assertFalse(destination.exists())

    def test_restore_never_overwrites_existing_directory(self):
        self.put(self.vault, 'note.md')
        archive = Path(backup(self.vault, self.state, self.archives)['archive'])
        destination = self.root / 'existing'
        destination.mkdir()
        with self.assertRaises(BackupError):
            restore(archive, destination)
        self.assertEqual(list(destination.iterdir()), [])

    def test_retention_keeps_latest_per_day_and_week(self):
        self.put(self.vault, 'note.md')
        today = datetime(2026, 10, 7, 12, tzinfo=ZoneInfo('America/Sao_Paulo'))
        latest = {}
        for index in range(28):
            moment = today - timedelta(days=27-index)
            for hour in [8, 20]:
                moment = moment.replace(hour=hour)
                with patch('mwsecondbrain.backup._now', return_value=moment):
                    latest[moment.date()] = Path(backup(self.vault, self.state, self.archives)['archive'])
        weekly = {}
        for date, path in reversed(list(latest.items())):
            weekly.setdefault(date.isocalendar()[:2], path)
        expected = set(list(latest.values())[-7:]) | set(list(weekly.values())[:4])
        self.assertEqual(set(self.archives.glob('mwsb-backup-*.tar.gz')), expected)

    def test_atomic_publication_never_replaces_existing_empty_directory(self):
        from mwsecondbrain.backup import _rename_without_overwrite
        source, destination = self.root / 'payload', self.root / 'destination'
        source.mkdir()
        destination.mkdir()
        self.put(source, 'file.md')
        with self.assertRaises(BackupError):
            _rename_without_overwrite(source, destination)
        self.assertTrue((source / 'file.md').exists())
        self.assertEqual(list(destination.iterdir()), [])

    def test_retention_keeps_daily_weekly_and_foreign_files(self):
        self.put(self.vault, 'note.md')
        self.archives.mkdir()
        self.put(self.archives, 'other.tar.gz')
        today = datetime(2026, 10, 7, 12, tzinfo=ZoneInfo('America/Sao_Paulo'))
        dates = []
        for index in range(35):
            moment = today - timedelta(days=34-index)
            dates.append(moment)
            with patch('mwsecondbrain.backup._now', return_value=moment):
                backup(self.vault, self.state, self.archives)
        owned = list(self.archives.glob('mwsb-backup-*.tar.gz'))
        self.assertLessEqual(len(owned), 11)
        self.assertGreaterEqual(len(owned), 7)
        self.assertTrue((self.archives / 'other.tar.gz').exists())
        kept_dates = {path.name[len('mwsb-backup-'):][:8] for path in owned}
        for moment in dates[-7:]:
            self.assertIn(moment.strftime('%Y%m%d'), kept_dates)


if __name__ == '__main__':
    unittest.main()
