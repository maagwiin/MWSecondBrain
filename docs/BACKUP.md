# Local backups and recovery

`mwsecondbrain.backup.backup(vault, state_dir, backup_dir)` creates a local
gzip tar archive. The controller must hold its exclusive lock and confirm that
the editor has stopped before calling it. This module does not start, stop,
or lock the editor. Store backups outside all source directories.

The snapshot includes vault notes, ignored files, Git history, `.obsidian`,
assets, state files and attachments. SQLite databases are identified by their
file header and copied with SQLite's online backup API. Committed WAL data is
included; WAL, SHM and journal sidecars are excluded. Snapshot databases use
DELETE journal mode. Optional `MWSB_OBSIDIAN_CONFIG_DIR` adds the configured
Obsidian directory. It must exist and must not be a symlink.
Its top-level `.XDG` runtime sockets and `.cache` are excluded; persistent
`.config` settings and vault `.obsidian` files remain included.

Archive paths retain `vault/`, `state/`, and optional `obsidian/` prefixes.
`manifest.json` records creation time and SHA-256 checksums for every payload
file. Archive creation uses a private staging directory and an atomic rename;
completed archives have mode `0600`. Any symlink or special file encountered
inside a source causes backup failure. Symlink source parents are rejected.
Directories excluded as credential locations are not traversed.

Excluded names are case insensitive: `.env*`, `auth.json`, `credentials`,
`credentials.json`, `*.key`, `*.pem`, `.aws`, `.ssh`, and `.gnupg`.
Global Codex credentials are never requested or added as a source. This name
filter cannot identify secrets embedded in ordinary notes or application
configuration. Keep such data outside the selected sources.

After a successful archive, retention keeps the latest snapshot from each of
the seven newest distinct dates and four newest ISO weeks, using
`America/Sao_Paulo`. These sets overlap. Only archives bearing this module's
filename format and producer manifest are candidates. Failed snapshots never
prune earlier archives. Unrelated files remain untouched.

The success result contains `state`, `message`, `last_success_at` (ISO 8601),
and `archive`. Invalid input raises `BackupError`; filesystem or SQLite errors
may propagate. Controllers should retain the previous successful timestamp
when reporting failure. Retention failure reports success with a cleanup
warning because the completed archive remains valid.

## Restore into isolation

Call `restore(archive, destination)` with a **nonexistent** destination whose
parent exists. Recovery creates a new isolated tree; it never writes into the
active vault. Stop the editor and use controller-controlled recovery before
promoting restored files to active storage.

Restore rejects absolute paths, traversal, unexpected prefixes, duplicates,
symlinks, hardlinks, devices and other nonregular entries. The manifest must
list exactly all payload files, and all checksums must match before publication.
No tar extraction helpers or archived permission bits are used. Files have
mode `0600`; staging is private. Malformed archives leave no destination.

Atomic publication uses Linux `renameat2(RENAME_NOREPLACE)` through Python's
standard-library `ctypes`. This also rejects a destination created during
validation, including an empty directory. Unsupported platforms fail closed.
The result adds `destination` to success metadata.

Run focused tests with:

```sh
rtk bash -c 'PYTHONPATH=src python3 -B -m unittest discover -s tests -p test_backup.py'
```
