"""The sole coordinator for vault writers, editor lifecycle and scheduled work."""

import fcntl
import importlib
import json
import os
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .db import Database


class UnsafeOperation(Exception):
    """An operation cannot safely proceed; HTTP callers receive a conflict."""


class EditorHelper:
    path = Path("/usr/local/libexec/mwsb-editor")

    @property
    def available(self):
        return self.path.is_file()

    def _run(self, verb):
        if verb not in {"start", "stop", "status"}:
            raise UnsafeOperation("Invalid editor operation")
        try:
            result = subprocess.run(["sudo", "-n", str(self.path), verb], check=True,
                                    capture_output=True, text=True, timeout=120)
            return json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            raise UnsafeOperation("Editor helper failed; writer state must be confirmed") from None

    def status(self):
        try:
            state = self._run("status").get("state")
            return state if state in {"running", "stopped"} else "unknown"
        except (UnsafeOperation, AttributeError):
            return "unknown"

    def start(self):
        self._run("start")

    def stop(self):
        self._run("stop")


def production_sync(vault):
    try:
        callback = importlib.import_module(".sync", __package__).sync
    except (ImportError, AttributeError):
        raise UnsafeOperation("Production sync adapter unavailable") from None
    return callback(vault)


def production_backup(vault, state_dir, backup_dir):
    try:
        callback = importlib.import_module(".backup", __package__).backup
    except (ImportError, AttributeError):
        raise UnsafeOperation("Production backup adapter unavailable") from None
    return callback(vault, state_dir, backup_dir)


class Controller:
    def __init__(self, settings: Settings, database: Database, editor=None,
                 sync_callback=None, backup_callback=None, clock=time.time):
        self.settings = settings
        self.database = database
        self.editor = editor if editor is not None else EditorHelper()
        self.sync_callback = sync_callback if sync_callback is not None else production_sync
        self.backup_callback = backup_callback if backup_callback is not None else production_backup
        self.clock = clock
        self.lock_path = settings.state_dir / "controller.lock"
        self._recover()

    @contextmanager
    def exclusive(self):
        try:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise UnsafeOperation("Controller lock unavailable") from None
        locked = False
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                raise UnsafeOperation("Another controller operation is running") from None
            yield
        finally:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _editor_state(self):
        try:
            state = self.editor.status()
            return state if state in {"running", "stopped"} else "unknown"
        except Exception:
            return "unknown"

    def _recover(self):
        try:
            with self.exclusive():
                mode = self.database.get("mode", "error")
                if mode in {"editing", "transition"}:
                    # A restart cannot renew old editing authority or resume unfinished writes.
                    self.database.set("mode", "error")
                    self.database.set("backup_required", True)
                    try:
                        self.editor.stop()
                    except Exception:
                        pass
                    self._editor_state()
                elif mode != "agent" or self._editor_state() != "stopped":
                    self.database.set("mode", "error")
                    self.database.set("backup_required", True)
        except UnsafeOperation:
            # The other process owns recovery; this instance may only observe.
            pass

    def _require_stopped(self):
        if self._editor_state() != "stopped":
            self.database.set("mode", "error")
            self.database.set("backup_required", True)
            raise UnsafeOperation("Editor is running or its writer state is unknown")

    def _require_agent(self):
        if self.database.get("mode") != "agent":
            raise UnsafeOperation("Vault writes require agent mode")
        self._require_stopped()

    def _timestamp(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()

    def _sync(self):
        self._require_stopped()
        try:
            result = self.sync_callback(self.settings.vault)
            if not isinstance(result, dict) or not isinstance(result.get("state"), str):
                raise ValueError("Invalid sync result")
        except UnsafeOperation as error:
            self.database.set("sync", {"state": "error", "message": str(error), "last_synced_at": self.database.get("sync", {}).get("last_synced_at")})
            raise
        except Exception:
            self.database.set("sync", {"state": "error", "message": "Synchronization failed", "last_synced_at": self.database.get("sync", {}).get("last_synced_at")})
            raise UnsafeOperation("Synchronization failed") from None
        previous = self.database.get("sync", {})
        confirmed = (result["state"].upper() == "SYNCED" and result.get("pending") is False
                     and bool(result.get("head")) and result.get("remote_checked_now") is True)
        status = {"state": result["state"], "message": str(result.get("message", "")),
                  "last_synced_at": self._timestamp() if confirmed else previous.get("last_synced_at")}
        self.database.set("sync", status)
        return result

    def sync(self):
        with self.exclusive():
            self._require_agent()
            return self._sync()

    def _backup(self):
        self._require_stopped()
        try:
            result = self.backup_callback(self.settings.vault, self.settings.state_dir, self.settings.backup_dir)
            if not isinstance(result, dict) or result.get("state") not in {"ok", "success", "saved"}:
                raise ValueError("Backup success was not confirmed")
        except UnsafeOperation:
            raise
        except Exception:
            raise UnsafeOperation("Backup failed") from None
        self.database.set("backup", {"last_success_at": self._timestamp()})
        self.database.set("backup_required", False)
        return result

    def backup(self, *, scheduled_day=None):
        with self.exclusive():
            self._require_agent()
            if scheduled_day is not None:
                if self.database.get("backup_attempt_day") == scheduled_day:
                    return None
                self.database.set("backup_attempt_day", scheduled_day)
            return self._backup()

    def change_mode(self, target):
        if target not in {"agent", "editing"}:
            raise UnsafeOperation("Invalid mode")
        with self.exclusive():
            old = self.database.get("mode", "error")
            if old == target:
                expected = "running" if target == "editing" else "stopped"
                if self._editor_state() == expected:
                    return self.status()
                self.database.set("mode", "error")
                raise UnsafeOperation("Editor state does not match the requested mode")
            if old in {"transition", "error"} and target == "editing":
                raise UnsafeOperation("Recover agent mode before opening the editor")
            self.database.set("mode", "transition")
            try:
                if old == "editing" or target == "agent":
                    self.editor.stop()
                self._require_stopped()
                if target == "agent" and (old == "editing" or self.database.get("backup_required", False)):
                    self._backup()
                self._sync()
                if target == "editing":
                    if not self.editor.available:
                        raise UnsafeOperation("Editor helper unavailable")
                    self.database.set("backup_required", True)
                    self.editor.start()
                    if self._editor_state() != "running":
                        raise UnsafeOperation("Editor start was not confirmed")
                self.database.set("mode", target)
            except Exception as error:
                self.database.set("mode", "error")
                # Never authorize writes after partial start, even if start raised.
                self._editor_state()
                if isinstance(error, UnsafeOperation):
                    raise
                raise UnsafeOperation("Editor transition failed") from None
            return self.status()

    def close_editor_for_logout(self):
        """Revoke editing authority, stop safely, and leave saved changes for recovery."""
        with self.exclusive():
            mode = self.database.get("mode", "error")
            if mode == "agent" and self._editor_state() == "stopped":
                return
            self.database.set("mode", "error")
            self.database.set("backup_required", True)
            try:
                self.editor.stop()
            except Exception:
                raise UnsafeOperation("Editor shutdown could not be confirmed") from None
            self._require_stopped()

    def status(self):
        mode = self.database.get("mode", "error")
        if mode in {"agent", "editing"}:
            expected = "stopped" if mode == "agent" else "running"
            if self._editor_state() != expected:
                mode = "error"
        return {"mode": mode,
                "sync": self.database.get("sync", {"state": "unknown", "message": "No synchronization completed", "last_synced_at": None}),
                "backup": self.database.get("backup", {"last_success_at": None}),
                "phase2": {"ready": False, "reason": "Subscription inference has not passed the eligibility gate"},
                "editor_available": bool(self.editor.available)}
