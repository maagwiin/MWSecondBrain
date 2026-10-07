"""Local, credential-filtered snapshots. Caller must hold the controller lock."""
from __future__ import annotations

from datetime import datetime
from contextlib import closing
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import uuid
from zoneinfo import ZoneInfo


class BackupError(ValueError):
    """A snapshot or restore failed its safety checks."""


_PREFIX = 'mwsb-backup-'
_NAME = re.compile(r'^mwsb-backup-(\d{8}T\d{12}[+-]\d{4})-[0-9a-f]{32}\.tar\.gz$')
_ZONE = ZoneInfo('America/Sao_Paulo')


def _now() -> datetime:
    return datetime.now(_ZONE)


def _secret(path: Path) -> bool:
    return any(part.lower().startswith('.env') or part.lower() in {
        'auth.json', 'credentials', 'credentials.json', '.aws', '.ssh', '.gnupg',
    } or part.lower().endswith(('.key', '.pem')) for part in path.parts)


def _root(path: Path) -> Path:
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise BackupError('Symlink source or destination is forbidden')
    return path.resolve()


def _copy_tree(source: Path, target: Path, sqlite: bool = False) -> None:
    if not source.is_dir():
        raise BackupError(f'Source directory does not exist: {source}')
    for directory, dirs, files in os.walk(source, followlinks=False):
        base = Path(directory)
        for name in dirs + files:
            path = base / name
            relative = path.relative_to(source)
            # Reject links even when their names would otherwise be excluded.
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise BackupError(f'Symlink inside source: {relative}')
            if _secret(relative):
                if name in dirs:
                    dirs.remove(name)
                continue
            if stat.S_ISDIR(mode):
                continue
            if not stat.S_ISREG(mode):
                raise BackupError(f'Nonregular source file: {relative}')
            if sqlite and name.endswith(('-wal', '-shm', '-journal')):
                continue
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, 'rb') as reader:
                header = reader.read(16)
                reader.seek(0)
                if sqlite and header == b'SQLite format 3\x00':
                    # Online backup includes committed WAL pages without copying sidecars.
                    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
                        with closing(sqlite3.connect(destination)) as snapshot:
                            connection.backup(snapshot)
                            snapshot.execute('PRAGMA journal_mode=DELETE')
                else:
                    with destination.open('wb') as writer:
                        shutil.copyfileobj(reader, writer)


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as reader:
        for chunk in iter(lambda: reader.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _prune(backup_dir: Path) -> None:
    snapshots = []
    for path in backup_dir.iterdir():
        match = _NAME.fullmatch(path.name)
        if not match or path.is_symlink() or not path.is_file():
            continue
        try:
            with tarfile.open(path, 'r:gz') as archive:
                reader = archive.extractfile('manifest.json')
                manifest = json.load(reader) if reader else {}
            if manifest.get('producer') != 'mwsecondbrain' or manifest.get('version') != 1:
                continue
            moment = datetime.fromisoformat(manifest['created_at']).astimezone(_ZONE)
            snapshots.append((moment, path))
        except (OSError, EOFError, tarfile.TarError, ValueError, KeyError, TypeError):
            continue
    snapshots.sort(reverse=True)
    daily, weekly = {}, {}
    for moment, path in snapshots:
        daily.setdefault(moment.date(), path)
        weekly.setdefault(moment.isocalendar()[:2], path)
    keep = set(list(daily.values())[:7]) | set(list(weekly.values())[:4])
    for _, path in snapshots:
        if path not in keep:
            path.unlink()


def backup(vault: Path, state_dir: Path, backup_dir: Path) -> dict:
    """Snapshot editor-stopped vault and state under caller's exclusive lock.

    Returns success metadata. Raises BackupError on invalid input. No retention
    deletion runs until a complete archive has been published atomically.
    """
    vault, state_dir, backup_dir = map(_root, (vault, state_dir, backup_dir))
    sources = [(vault, 'vault'), (state_dir, 'state')]
    config = os.environ.get('MWSB_OBSIDIAN_CONFIG_DIR')
    if config:
        sources.append((_root(Path(config).expanduser()), 'obsidian'))
    for source, _ in sources:
        if source == backup_dir or source in backup_dir.parents or backup_dir in source.parents:
            raise BackupError('Backup directory must be isolated from source directories')
    backup_dir.mkdir(parents=True, exist_ok=True)
    moment = _now().astimezone(_ZONE)
    stamp = moment.strftime('%Y%m%dT%H%M%S%f%z')
    archive_path = backup_dir / f'{_PREFIX}{stamp}-{uuid.uuid4().hex}.tar.gz'
    with tempfile.TemporaryDirectory(prefix='.mwsb-stage-', dir=backup_dir) as temporary:
        staging = Path(temporary)
        payload = staging / 'payload'
        payload.mkdir()
        for source, prefix in sources:
            _copy_tree(source, payload / prefix, sqlite=prefix == 'state')
        files = {path.relative_to(payload).as_posix(): _checksum(path)
                 for path in sorted(payload.rglob('*')) if path.is_file()}
        manifest = {'version': 1, 'producer': 'mwsecondbrain',
                    'created_at': moment.isoformat(), 'files': files}
        (payload / 'manifest.json').write_text(json.dumps(manifest, sort_keys=True), encoding='utf-8')
        staged_archive = staging / 'archive.tar.gz'
        with tarfile.open(staged_archive, 'w:gz') as archive:
            for name in [*files, 'manifest.json']:
                archive.add(payload / name, arcname=name, recursive=False)
        staged_archive.chmod(0o600)
        with staged_archive.open('rb') as reader:
            os.fsync(reader.fileno())
        os.replace(staged_archive, archive_path)
    message = 'Backup completed'
    try:
        _prune(backup_dir)
    except OSError:
        message = 'Backup completed; retention cleanup failed'
    return {'state': 'success', 'message': message,
            'last_success_at': moment.isoformat(), 'archive': str(archive_path)}


def _member_name(name: str) -> str:
    path = PurePosixPath(name)
    if (not name or '\\' in name or path.is_absolute() or '..' in path.parts
            or path.as_posix() != name or ':' in name):
        raise BackupError('Unsafe archive path')
    if name != 'manifest.json' and path.parts[0] not in {'vault', 'state', 'obsidian'}:
        raise BackupError('Unexpected archive prefix')
    return name


def _rename_without_overwrite(source: Path, destination: Path) -> None:
    """Linux renameat2 protects against a destination created during validation."""
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, 'renameat2', None)
    if rename is None:
        raise BackupError('Atomic restore requires Linux renameat2 support')
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise BackupError(f'Atomic restore publication failed: {os.strerror(error)}')


def restore(archive: Path, destination: Path) -> dict:
    """Validate then restore into a new, nonexistent isolated directory."""
    archive, destination = _root(archive), _root(destination)
    if destination.exists():
        raise BackupError('Restore destination must not exist')
    if not destination.parent.is_dir():
        raise BackupError('Restore destination parent must exist')
    if destination in archive.parents:
        raise BackupError('Restore destination must be isolated from archive')
    with tempfile.TemporaryDirectory(prefix='.mwsb-restore-', dir=destination.parent) as temporary:
        staging = Path(temporary) / 'payload'
        staging.mkdir()
        try:
            with tarfile.open(archive, 'r:gz') as reader:
                members = reader.getmembers()
                names = set()
                for member in members:
                    name = _member_name(member.name)
                    if name in names or not member.isfile():
                        raise BackupError('Duplicate or nonregular archive entry')
                    names.add(name)
                manifest_reader = reader.extractfile('manifest.json')
                manifest = json.load(manifest_reader) if manifest_reader else None
                if not isinstance(manifest, dict) or manifest.get('version') != 1:
                    raise BackupError('Invalid backup manifest')
                expected = manifest.get('files')
                if not isinstance(expected, dict) or set(expected) != names - {'manifest.json'}:
                    raise BackupError('Manifest file set does not match archive')
                for member in members:
                    if member.name == 'manifest.json':
                        continue
                    path = staging / member.name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    content = reader.extractfile(member)
                    if content is None:
                        raise BackupError('Missing archive content')
                    with content, path.open('xb') as writer:
                        shutil.copyfileobj(content, writer)
                    path.chmod(0o600)
                    if _checksum(path) != expected[member.name]:
                        raise BackupError('Backup checksum mismatch')
            # Recheck prevents replacing an empty directory created during validation.
            if destination.exists() or destination.is_symlink():
                raise BackupError('Restore destination appeared during validation')
            _rename_without_overwrite(staging, destination)
        except (tarfile.TarError, EOFError, OSError, ValueError, KeyError, TypeError) as error:
            if isinstance(error, BackupError):
                raise
            raise BackupError(f'Invalid backup archive: {error}') from error
    return {'state': 'success', 'message': 'Restore completed',
            'archive': str(archive), 'destination': str(destination),
            'last_success_at': _now().isoformat()}
