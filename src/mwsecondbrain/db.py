"""Durable private state. Connections are short-lived and thread independent."""

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Database:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.path = self.state_dir / "state.sqlite3"
        if self.path.is_symlink():
            raise ValueError("State database must not be a symbolic link")
        descriptor = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        os.chmod(self.path, 0o600)
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL,
                    created REAL NOT NULL, touched REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_attempts (
                    ip TEXT PRIMARY KEY, failures INTEGER NOT NULL, started REAL NOT NULL
                );
                INSERT OR IGNORE INTO metadata VALUES ('mode', '"agent"');
                CREATE TABLE IF NOT EXISTS chat_conversations (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, capture_paused INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, job_id TEXT NOT NULL,
                    role TEXT NOT NULL, content TEXT NOT NULL, origin TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, attachments TEXT NOT NULL DEFAULT '[]'
                );
                CREATE TABLE IF NOT EXISTS chat_jobs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                    conversation_id TEXT NOT NULL, message_id TEXT NOT NULL, assistant_id TEXT,
                    origin TEXT NOT NULL, idempotency_key TEXT NOT NULL, model TEXT,
                    attachment_ids TEXT NOT NULL, external_reply TEXT, status TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0, capture_denied INTEGER NOT NULL DEFAULT 0,
                    error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(origin,idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS chat_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL UNIQUE,
                    external_reply TEXT NOT NULL, content TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS note_snapshots (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, available INTEGER NOT NULL, reason TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS note_snapshot_files (
                    snapshot_id TEXT NOT NULL, path TEXT NOT NULL, content TEXT NOT NULL,
                    content_hash TEXT NOT NULL, PRIMARY KEY(snapshot_id,path)
                );
                CREATE TABLE IF NOT EXISTS note_operations (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, job_id TEXT NOT NULL,
                    path TEXT NOT NULL, base_hash TEXT, new_hash TEXT NOT NULL, content TEXT NOT NULL,
                    reason TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def connect(self, immediate=False):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, key, default=None):
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connect(immediate=True) as connection:
            connection.execute("INSERT INTO metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (key, json.dumps(value)))
