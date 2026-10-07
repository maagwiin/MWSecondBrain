"""Consistent read snapshots and constrained, hash-checked Markdown proposals."""

import hashlib
import importlib.util
import json
import os
import stat
import time
import uuid
from pathlib import Path, PurePosixPath

from .chat import timestamp

NOTE_LIMIT = 512 * 1024
GUIDANCE_LIMIT = 32 * 1024
GUIDANCE_FILES = ("AGENTS.md", "SKILL.md", "README.md")
PROTECTED = {"agents.md", "claude.md", "gemini.md", "skill.md", "system.md", "instructions.md",
             "readme.md", "codex.md", "soul.md", "identity.md", "tools.md"}


class NoteRejected(ValueError):
    pass


def safe_read_path(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 512 or "\\" in value or any(ord(c) < 32 for c in value):
        raise NoteRejected("Invalid note path")
    parts = value.split("/")
    if any(not part or part in {".", ".."} or part.startswith(".") for part in parts):
        raise NoteRejected("Traversal and hidden paths are not permitted")
    path = PurePosixPath(value)
    if path.is_absolute() or path.suffix.lower() != ".md":
        raise NoteRejected("Only nonhidden Markdown paths can be read")
    return value


def safe_path(value):
    safe_read_path(value)
    if PurePosixPath(value).name.casefold() in PROTECTED:
        raise NoteRejected("Instruction files are protected and cannot be changed")
    return value


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load_private_policy(vault):
    script = Path(os.environ.get("MWSB_SYNC_SCRIPT", "/opt/mwsecondbrain/private-sync/brain_sync.py"))
    path = script.with_name("brain_policy.py")
    try:
        if not path.is_absolute() or path.is_symlink() or Path(vault) in path.resolve().parents:
            return None
        for item in [path, *path.parents]:
            info = item.stat()
            if item.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
                return None
        spec = importlib.util.spec_from_file_location("mwsb_trusted_note_policy", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not callable(module.path_problem) or not callable(module.content_problem):
            return None
        return lambda name, data: module.path_problem(name) or module.content_problem(data)
    except Exception:
        return None


class NotesService:
    def __init__(self, controller, policy=False):
        self.controller = controller
        self.database = controller.database
        self.vault = controller.settings.vault
        self.policy = load_private_policy(self.vault) if policy is False else policy

    def _scan(self, path, data):
        if self.policy is None:
            raise NoteRejected("Trusted secret scanner unavailable; note writes are disabled")
        try:
            problem = self.policy(path, data)
        except Exception:
            raise NoteRejected("Trusted secret scanner failed") from None
        if problem:
            raise NoteRejected(str(problem))

    def _files(self):
        for directory, directories, files in os.walk(self.vault, followlinks=False):
            directories[:] = sorted(name for name in directories if not name.startswith(".") and not (Path(directory) / name).is_symlink())
            for name in sorted(files):
                path = Path(directory) / name
                relative = path.relative_to(self.vault).as_posix()
                try:
                    safe_read_path(relative)
                except NoteRejected:
                    continue
                if path.is_symlink() or not path.is_file():
                    raise NoteRejected("Symbolic links and special files cannot be notes")
                with path.open("rb") as source:
                    data = source.read(NOTE_LIMIT + 1)
                if len(data) > NOTE_LIMIT:
                    raise NoteRejected("Note exceeds the safe content limit")
                yield relative, data

    def capture_snapshot(self):
        with self.controller.exclusive():
            self.controller._require_stopped()
            return self.capture_snapshot_locked()

    def capture_snapshot_locked(self):
        self.controller._require_stopped()
        files = []
        available = self.policy is not None
        if available:
            try:
                for path, data in self._files():
                    try:
                        self._scan(path, data)
                        content = data.decode("utf-8")
                    except (NoteRejected, UnicodeError):
                        continue  # Unsafe content is not exposed to the model.
                    files.append((path, content, digest(data)))
            except (NoteRejected, OSError):
                available = False
                files = []
        fingerprint = hashlib.sha256(b"available" if available else b"unavailable")
        for path, content, content_hash in files:
            fingerprint.update(path.encode() + b"\0" + content_hash.encode() + b"\0")
        identifier = fingerprint.hexdigest()
        with self.database.connect(immediate=True) as connection:
            connection.execute("INSERT OR IGNORE INTO note_snapshots VALUES (?,?,?,?)", (identifier, timestamp(self.controller.clock), int(available), "" if available else "Trusted scanner or safe snapshot unavailable"))
            connection.executemany("INSERT OR IGNORE INTO note_snapshot_files VALUES (?,?,?,?)", [(identifier, *item) for item in files])
            connection.execute("INSERT INTO metadata VALUES ('notes_snapshot',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(identifier),))
        return identifier

    def snapshot_for_runtime(self):
        with self.controller.exclusive():
            mode = self.database.get("mode", "error")
            if mode == "agent":
                self.controller._require_stopped()
                return self.capture_snapshot_locked()
            # No reading the changing vault while Obsidian is open or state is uncertain.
            snapshot = self.database.get("notes_snapshot")
            if snapshot is None:
                raise NoteRejected("No consistent vault snapshot is available")
            return snapshot

    def read(self, snapshot, path):
        safe_read_path(path)
        with self.database.connect() as connection:
            metadata = connection.execute("SELECT available FROM note_snapshots WHERE id=?", (snapshot,)).fetchone()
            if not metadata or not metadata[0]:
                raise NoteRejected("Trusted scanner or safe snapshot unavailable")
            row = connection.execute("SELECT content,content_hash FROM note_snapshot_files WHERE snapshot_id=? AND path=?", (snapshot, path)).fetchone()
        return {"path": path, "content": row[0] if row else "", "hash": row[1] if row else None, "exists": row is not None}

    def guidance(self, snapshot):
        """Bounded root guidance from the same immutable read snapshot as tools."""
        records = []
        for name in GUIDANCE_FILES:
            try:
                note = self.read(snapshot, name)
                if not note["exists"]:
                    continue
                data = note["content"].encode("utf-8")
                self._scan(name, data)
                records.append((name, data))
            except NoteRejected:
                continue
        if not records:
            return ""
        labels = {name: f"\n### Read-only brain guidance: {name}\n".encode() for name, _ in records}
        truncation = b"\n[Truncated to the guidance limit]\n"
        budget = GUIDANCE_LIMIT - sum(len(label) + len(truncation) + 1 for label in labels.values())
        allocations = {}
        # Give small files their full allowance before sharing remaining space.
        for index, (name, data) in enumerate(sorted(records, key=lambda item: len(item[1]))):
            allocation = min(len(data), budget // (len(records) - index))
            allocations[name] = allocation
            budget -= allocation
        sections = []
        for name, data in records:
            bounded = data[:allocations[name]].decode("utf-8", errors="ignore").encode()
            sections.append(labels[name] + bounded + (truncation if len(bounded) < len(data) else b"\n"))
        result = b"".join(sections)
        try:
            self._scan("guidance.md", result)
        except NoteRejected:
            return ""
        return result.decode("utf-8")

    def search(self, snapshot, query, limit=10):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 256:
            raise NoteRejected("Invalid search query")
        limit = max(1, min(int(limit), 50))
        terms = query.casefold().split()
        matches = []
        with self.database.connect() as connection:
            metadata = connection.execute("SELECT available FROM note_snapshots WHERE id=?", (snapshot,)).fetchone()
            if not metadata or not metadata[0]:
                raise NoteRejected("Trusted scanner or safe snapshot unavailable")
            for row in connection.execute("SELECT path,content,content_hash FROM note_snapshot_files WHERE snapshot_id=? ORDER BY path", (snapshot,)):
                if all(term in (row[0] + " " + row[1]).casefold() for term in terms):
                    matches.append({"path": row[0], "snippet": row[1][:1000], "hash": row[2]})
                    if len(matches) == limit:
                        break
        return {"matches": matches, "snapshot_id": snapshot}

    def propose(self, job_id, path, content, base_hash, reason, snapshot):
        safe_path(path)
        if not isinstance(content, str) or not content.strip() or len(content.encode()) > NOTE_LIMIT:
            raise NoteRejected("Empty or oversized note content is not permitted")
        if not isinstance(reason, str) or not 1 <= len(reason) <= 1000:
            raise NoteRejected("A proposal reason is required")
        self._scan(path, content.encode())
        original = self.read(snapshot, path)
        if base_hash != original["hash"]:
            raise NoteRejected("Proposal does not match the snapshot base hash")
        identifier, now = uuid.uuid4().hex, timestamp(self.controller.clock)
        with self.database.connect(immediate=True) as connection:
            job = connection.execute("SELECT j.*,c.capture_paused FROM chat_jobs j JOIN chat_conversations c ON c.id=j.conversation_id WHERE j.id=?", (job_id,)).fetchone()
            if not job or job["status"] != "running" or job["cancel_requested"] or job["capture_denied"] or job["capture_paused"]:
                raise NoteRejected("Capture is paused or denied for this turn")
            existing = connection.execute("SELECT id,status FROM note_operations WHERE job_id=? AND path=? AND new_hash=?", (job_id, path, digest(content.encode()))).fetchone()
            if existing:
                return {"operation_id": existing[0], "status": existing[1]}
            if connection.execute("SELECT count(*) FROM note_operations WHERE job_id=?", (job_id,)).fetchone()[0] >= 8:
                raise NoteRejected("Proposal limit reached for this turn")
            connection.execute("INSERT INTO note_operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (identifier, job["conversation_id"], job_id, path, base_hash, digest(content.encode()), content, reason, "proposed", None, now, now))
            self._event(connection, job["conversation_id"], job_id, identifier, "proposed")
        return {"operation_id": identifier, "status": "proposed"}

    def _event(self, connection, conversation, job_id, operation_id, status):
        connection.execute("INSERT INTO chat_events(payload) VALUES (?)", (json.dumps({"type": "operation", "conversation_id": conversation, "job_id": job_id, "operation_id": operation_id, "status": status}),))

    def _set_status(self, operation, status, error=None):
        with self.database.connect(immediate=True) as connection:
            connection.execute("UPDATE note_operations SET status=?,error=?,updated_at=? WHERE id=?", (status, error, timestamp(self.controller.clock), operation["id"]))
            self._event(connection, operation["conversation_id"], operation["job_id"], operation["id"], status)

    def pending_count(self):
        with self.database.connect() as connection:
            return connection.execute("SELECT count(*) FROM note_operations WHERE status IN ('queued','applying')").fetchone()[0]

    def _full_scan(self, path, data):
        self._scan(path, data)
        for existing, content in self._files():
            self._scan(existing, content)

    def _parent(self, path):
        """Walk from an open vault descriptor without following any symlink."""
        parts = safe_path(path).split("/")
        descriptor = os.open(self.vault, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                except FileNotFoundError:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return descriptor, parts[-1]
        except OSError:
            os.close(descriptor)
            raise NoteRejected("Note parent is unsafe or contains a symbolic link") from None

    def _current_hash(self, parent, name):
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        except FileNotFoundError:
            return None
        except OSError:
            raise NoteRejected("Note target is unsafe or contains a symbolic link") from None
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise NoteRejected("Only regular note files can be changed")
            data = source.read(NOTE_LIMIT + 1)
            if len(data) > NOTE_LIMIT:
                raise NoteRejected("Note target exceeds content limit")
            return digest(data)

    def apply_pending_locked(self):
        self.controller._require_stopped()
        result = {"applied": 0, "rejected": 0, "queued": 0}
        with self.database.connect() as connection:
            operations = [dict(row) for row in connection.execute("SELECT * FROM note_operations WHERE status IN ('queued','applying') ORDER BY rowid")]
        backed_up = False
        for operation in operations:
            parent, temporary = None, None
            try:
                with self.database.connect() as connection:
                    allowed = connection.execute("""SELECT j.status,j.capture_denied,c.capture_paused FROM chat_jobs j
                        JOIN chat_conversations c ON c.id=j.conversation_id WHERE j.id=?""", (operation["job_id"],)).fetchone()
                if not allowed or allowed[0] != "completed" or allowed[1] or allowed[2]:
                    raise NoteRejected("Capture is paused or denied for this turn")
                self._full_scan(operation["path"], operation["content"].encode())
                parent, name = self._parent(operation["path"])
                current = self._current_hash(parent, name)
                if current != operation["new_hash"]:
                    if current != operation["base_hash"]:
                        raise NoteRejected("Original note changed; stale proposal was not applied")
                    if not backed_up:
                        try:
                            self.controller._backup()
                        except Exception:
                            self._set_status(operation, "queued", "Backup before note changes failed")
                            raise
                        backed_up = True
                    self._set_status(operation, "applying")
                    self.controller._require_stopped()
                    temporary = ".mwsb-" + operation["id"] + ".tmp"
                    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                    with os.fdopen(descriptor, "wb") as target:
                        target.write(operation["content"].encode())
                        target.flush()
                        os.fsync(target.fileno())
                    self.controller._require_stopped()
                    if self._current_hash(parent, name) != operation["base_hash"]:
                        raise NoteRejected("Original note changed before replacement")
                    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                    temporary = None
                    os.fsync(parent)
                self._set_status(operation, "applied")
                result["applied"] += 1
            except NoteRejected as error:
                self._set_status(operation, "rejected", str(error))
                result["rejected"] += 1
            except OSError:
                self._set_status(operation, "queued", "Note storage failed; retry is required")
                result["queued"] += 1
            finally:
                if parent is not None:
                    if temporary is not None:
                        try:
                            os.unlink(temporary, dir_fd=parent)
                        except OSError:
                            pass
                    os.close(parent)
        return result


class NoteTools:
    def __init__(self, service, store, job_id, snapshot, cancelled=lambda: False):
        self.service, self.store, self.job_id, self.snapshot = service, store, job_id, snapshot
        self.cancelled = cancelled

    def __call__(self, name, arguments):
        try:
            if self.cancelled() or self.store.cancelled(self.job_id):
                raise NoteRejected("Turn is cancelled; tools are disabled")
            if not isinstance(arguments, dict):
                raise NoteRejected("Invalid tool arguments")
            if name == "brain_search":
                return self.service.search(self.snapshot, arguments.get("query"), arguments.get("limit", 10))
            if name == "brain_read":
                return self.service.read(self.snapshot, arguments.get("path"))
            if name == "brain_propose_update":
                return self.service.propose(self.job_id, arguments.get("path"), arguments.get("content"), arguments.get("base_hash"), arguments.get("reason"), self.snapshot)
            raise NoteRejected("Tool is not permitted")
        except (NoteRejected, TypeError, ValueError) as error:
            return {"error": str(error)}
