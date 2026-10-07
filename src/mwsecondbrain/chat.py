"""Durable unified conversation, sequential subscription worker and delivery outbox."""

import asyncio
import copy
import fcntl
import json
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from .codex import AuthRequired, QuotaExceeded, RuntimeFailure, Cancelled
except ImportError:
    # Phase one can run without the optional subscription process adapter.
    class AuthRequired(Exception):
        pass
    class QuotaExceeded(Exception):
        pass
    class RuntimeFailure(Exception):
        pass
    class Cancelled(Exception):
        pass


def timestamp(clock=time.time):
    return datetime.fromtimestamp(clock(), timezone.utc).isoformat()


class ChatStore:
    def __init__(self, database, clock=time.time):
        self.database = database
        self.clock = clock
        with database.connect(immediate=True) as connection:
            if not connection.execute("SELECT 1 FROM metadata WHERE key='chat_active_conversation'").fetchone():
                self._new_conversation(connection)

    def _event(self, connection, kind, conversation_id, job_id=None, **fields):
        payload = {"type": kind, "conversation_id": conversation_id, **fields}
        if job_id:
            payload["job_id"] = job_id
        connection.execute("INSERT INTO chat_events(payload) VALUES (?)", (json.dumps(payload),))

    def _active(self, connection):
        return json.loads(connection.execute("SELECT value FROM metadata WHERE key='chat_active_conversation'").fetchone()[0])

    def _new_conversation(self, connection):
        identifier = uuid.uuid4().hex
        connection.execute("INSERT INTO chat_conversations VALUES (?,?,0)", (identifier, timestamp(self.clock)))
        connection.execute("INSERT INTO metadata VALUES ('chat_active_conversation',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(identifier),))
        self._event(connection, "conversation", identifier)
        return identifier

    def enqueue(self, text, origin, idempotency_key, attachment_ids=None, model=None, external_reply=None):
        if not isinstance(text, str) or len(text) > 32768 or origin not in {"web", "telegram"}:
            raise ValueError("Invalid chat message")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 256:
            raise ValueError("An idempotency key is required")
        attachments = list(attachment_ids or [])
        if len(attachments) > 8 or any(not isinstance(item, str) or not 1 <= len(item) <= 128 for item in attachments):
            raise ValueError("Invalid attachment IDs")
        if not text.strip() and not attachments:
            raise ValueError("Message cannot be empty")
        if model is not None and (not isinstance(model, str) or not 1 <= len(model) <= 128):
            raise ValueError("Invalid model")
        now = timestamp(self.clock)
        with self.database.connect(immediate=True) as connection:
            existing = connection.execute("SELECT id,message_id FROM chat_jobs WHERE origin=? AND idempotency_key=?", (origin, idempotency_key)).fetchone()
            if existing:
                return {"job_id": existing[0], "message_id": existing[1]}
            conversation = self._active(connection)
            job_id, message_id = uuid.uuid4().hex, uuid.uuid4().hex
            capture_denied = bool(re.search(r"\b(?:não|nao)\s+guarde\b", text.casefold()))
            connection.execute("""INSERT INTO chat_jobs
                (id,conversation_id,message_id,origin,idempotency_key,model,attachment_ids,external_reply,status,capture_denied,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (job_id, conversation, message_id, origin, idempotency_key, model, json.dumps(attachments), json.dumps(external_reply) if external_reply is not None else None, "queued", int(capture_denied), now, now))
            connection.execute("INSERT INTO chat_messages VALUES (?,?,?,?,?,?,?,?,?)", (message_id, conversation, job_id, "user", text, origin, "queued", now, json.dumps(attachments)))
            self._event(connection, "queued", conversation, job_id)
            return {"job_id": job_id, "message_id": message_id}

    def lookup_idempotency(self, key, origin="telegram"):
        with self.database.connect() as connection:
            row = connection.execute("SELECT id,message_id FROM chat_jobs WHERE origin=? AND idempotency_key=?", (origin, key)).fetchone()
            return {"job_id": row[0], "message_id": row[1]} if row else None

    def view(self, conversation_id=None):
        with self.database.connect() as connection:
            identifier = conversation_id or self._active(connection)
            conversation = connection.execute("SELECT * FROM chat_conversations WHERE id=?", (identifier,)).fetchone()
            if not conversation:
                raise ValueError("Conversation not found")
            messages = []
            for row in connection.execute("SELECT * FROM chat_messages WHERE conversation_id=? ORDER BY rowid", (identifier,)):
                message = dict(row)
                message["attachments"] = json.loads(message["attachments"])
                messages.append(message)
            jobs = []
            for row in connection.execute("""SELECT j.id,j.message_id,j.status,j.origin,j.model,j.error,j.cancel_requested,j.created_at,
                o.status AS delivery_status FROM chat_jobs j LEFT JOIN chat_outbox o ON o.job_id=j.id
                WHERE j.conversation_id=? ORDER BY j.sequence""", (identifier,)):
                job = dict(row)
                job["cancel_requested"] = bool(job["cancel_requested"])
                jobs.append(job)
            operations = [dict(row) for row in connection.execute("SELECT id,job_id,path,status,reason,error,created_at FROM note_operations WHERE conversation_id=? ORDER BY rowid", (identifier,))]
        return {"conversation_id": identifier, "capture_paused": bool(conversation["capture_paused"]), "messages": messages, "jobs": jobs, "operations": operations, "runtime": self.runtime_state(), "telegram": self.database.get("telegram_status", {"state": "disabled", "message": "Telegram not configured"})}

    def conversations(self):
        with self.database.connect() as connection:
            active = self._active(connection)
            return [{"id": row[0], "created_at": row[1], "active": row[0] == active} for row in connection.execute("SELECT id,created_at FROM chat_conversations ORDER BY rowid DESC")]

    def new_conversation(self):
        with self.database.connect(immediate=True) as connection:
            for row in connection.execute("SELECT * FROM chat_jobs WHERE status IN ('queued','running')").fetchall():
                if row["status"] == "queued":
                    connection.execute("UPDATE chat_jobs SET status='cancelled',cancel_requested=1,updated_at=? WHERE id=?", (timestamp(self.clock), row["id"]))
                    connection.execute("UPDATE chat_messages SET status='cancelled' WHERE job_id=?", (row["id"],))
                else:
                    connection.execute("UPDATE chat_jobs SET cancel_requested=1 WHERE id=?", (row["id"],))
                self._event(connection, "cancel_requested", row["conversation_id"], row["id"])
            return self._new_conversation(connection)

    def set_capture(self, paused):
        with self.database.connect(immediate=True) as connection:
            active = self._active(connection)
            connection.execute("UPDATE chat_conversations SET capture_paused=? WHERE id=?", (int(bool(paused)), active))
            self._event(connection, "capture", active, paused=bool(paused))

    def cancel(self, job_id):
        with self.database.connect(immediate=True) as connection:
            row = connection.execute("SELECT * FROM chat_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("Job not found")
            if row["status"] not in {"queued", "running"}:
                return False
            connection.execute("UPDATE chat_jobs SET cancel_requested=1,updated_at=? WHERE id=?", (timestamp(self.clock), job_id))
            if row["status"] == "queued":
                connection.execute("UPDATE chat_jobs SET status='cancelled' WHERE id=?", (job_id,))
                connection.execute("UPDATE chat_messages SET status='cancelled' WHERE job_id=?", (job_id,))
            self._event(connection, "cancel_requested", row["conversation_id"], job_id)
            return True

    def cancelled(self, job_id):
        with self.database.connect() as connection:
            row = connection.execute("SELECT status,cancel_requested FROM chat_jobs WHERE id=?", (job_id,)).fetchone()
            return not row or row[0] != "running" or bool(row[1])

    def claim_next(self):
        with self.database.connect(immediate=True) as connection:
            # The SQL guard also prevents parallel calls outside the normal worker lifecycle.
            if connection.execute("SELECT 1 FROM chat_jobs WHERE status='running'").fetchone():
                return None
            row = connection.execute("SELECT * FROM chat_jobs WHERE status='queued' ORDER BY sequence LIMIT 1").fetchone()
            if not row:
                return None
            now, assistant_id = timestamp(self.clock), uuid.uuid4().hex
            connection.execute("UPDATE chat_jobs SET status='running',assistant_id=?,updated_at=? WHERE id=?", (assistant_id, now, row["id"]))
            connection.execute("UPDATE chat_messages SET status='running' WHERE id=?", (row["message_id"],))
            connection.execute("INSERT INTO chat_messages VALUES (?,?,?,?,?,?,?,?,?)", (assistant_id, row["conversation_id"], row["id"], "assistant", "", row["origin"], "running", now, "[]"))
            self._event(connection, "running", row["conversation_id"], row["id"])
            job = dict(row)
            job.update(status="running", assistant_id=assistant_id)
            job["attachment_ids"] = json.loads(job["attachment_ids"])
            job["external_reply"] = json.loads(job["external_reply"]) if job["external_reply"] else None
            return job

    def context(self, job):
        with self.database.connect() as connection:
            rows = connection.execute("""SELECT m.role,m.content FROM chat_messages m JOIN chat_jobs j ON j.id=m.job_id
                WHERE m.conversation_id=? AND m.status='completed'
                ORDER BY j.sequence DESC,CASE m.role WHEN 'assistant' THEN 0 ELSE 1 END LIMIT 40""", (job["conversation_id"],)).fetchall()
            current = connection.execute("SELECT content FROM chat_messages WHERE id=?", (job["message_id"],)).fetchone()[0]
        selected, remaining = [], 65536 - len(current)
        for row in rows:
            if len(row[1]) > remaining:
                break
            selected.append({"role": row[0], "content": row[1]})
            remaining -= len(row[1])
        return list(reversed(selected)) + [{"role": "user", "content": current}]

    def delta(self, job_id, text):
        if not isinstance(text, str) or not text:
            return
        with self.database.connect(immediate=True) as connection:
            row = connection.execute("SELECT * FROM chat_jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] != "running" or row["cancel_requested"]:
                return
            length = connection.execute("SELECT length(content) FROM chat_messages WHERE id=?", (row["assistant_id"],)).fetchone()[0]
            if length + len(text) > 1048576:
                raise RuntimeFailure("Response limit exceeded")
            connection.execute("UPDATE chat_messages SET content=content||? WHERE id=?", (text, row["assistant_id"]))
            self._event(connection, "delta", row["conversation_id"], job_id, delta=text)

    def finish(self, job_id, status, content="", error=None):
        if status not in {"completed", "cancelled", "failed", "uncertain"}:
            raise ValueError("Invalid final job status")
        with self.database.connect(immediate=True) as connection:
            row = connection.execute("SELECT * FROM chat_jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] != "running":
                return False
            if row["cancel_requested"] and status == "completed":
                status = "cancelled"
            now = timestamp(self.clock)
            connection.execute("UPDATE chat_jobs SET status=?,error=?,updated_at=? WHERE id=?", (status, error, now, job_id))
            connection.execute("UPDATE chat_messages SET status=? WHERE job_id=?", (status, job_id))
            if status == "completed":
                connection.execute("UPDATE chat_messages SET content=? WHERE id=?", (content, row["assistant_id"]))
                connection.execute("UPDATE note_operations SET status='queued',updated_at=? WHERE job_id=? AND status='proposed'", (now, job_id))
                if row["origin"] == "telegram" and row["external_reply"] is not None:
                    connection.execute("INSERT OR IGNORE INTO chat_outbox(job_id,external_reply,content,status,created_at) VALUES (?,?,?,'pending',?)", (job_id, row["external_reply"], content, now))
            else:
                connection.execute("UPDATE note_operations SET status='rejected',error='Turn did not complete',updated_at=? WHERE job_id=? AND status='proposed'", (now, job_id))
            self._event(connection, status, row["conversation_id"], job_id)
            return status == "completed"

    def recover_running(self):
        with self.database.connect() as connection:
            jobs = [row[0] for row in connection.execute("SELECT id FROM chat_jobs WHERE status='running'")]
        for job_id in jobs:
            self.finish(job_id, "uncertain", error="Service restarted; explicit retry is required")

    def events_after(self, event_id, limit=200):
        with self.database.connect() as connection:
            return [{"id": row[0], **json.loads(row[1])} for row in connection.execute("SELECT id,payload FROM chat_events WHERE id>? ORDER BY id LIMIT ?", (event_id, min(max(limit, 1), 500)))]

    def runtime_state(self):
        return self.database.get("chat_runtime", {"state": "unavailable", "reason": "Subscription runtime unavailable"})

    def set_runtime(self, state, reason=""):
        self.database.set("chat_runtime", {"state": state, "reason": reason})

    def pending_outbox(self):
        with self.database.connect() as connection:
            return [{**dict(row), "external_reply": json.loads(row["external_reply"])} for row in connection.execute("SELECT * FROM chat_outbox WHERE status='pending' ORDER BY id")]

    def claim_outbox(self, identifier):
        with self.database.connect(immediate=True) as connection:
            changed = connection.execute("UPDATE chat_outbox SET status='uncertain' WHERE id=? AND status='pending'", (identifier,)).rowcount == 1
            if changed:
                row = connection.execute("SELECT j.conversation_id,j.id FROM chat_outbox o JOIN chat_jobs j ON j.id=o.job_id WHERE o.id=?", (identifier,)).fetchone()
                self._event(connection, "delivery", row[0], row[1], status="uncertain")
            return changed

    def mark_outbox_sent(self, identifier):
        with self.database.connect(immediate=True) as connection:
            row = connection.execute("SELECT j.conversation_id,j.id FROM chat_outbox o JOIN chat_jobs j ON j.id=o.job_id WHERE o.id=? AND o.status='uncertain'", (identifier,)).fetchone()
            if row:
                connection.execute("UPDATE chat_outbox SET status='sent' WHERE id=?", (identifier,))
                self._event(connection, "delivery", row[0], row[1], status="sent")


class ChatWorker:
    def __init__(self, store, controller, runtime=None, completion_hook=None,
                 attachments_resolver=None, verified=True):
        self.store, self.controller, self.runtime = store, controller, runtime
        self.completion_hook, self.attachments_resolver = completion_hook, attachments_resolver
        self.verified = verified
        self.catalog = []
        self.closing = threading.Event()
        self.task = None
        self.lock_descriptor = None
        self.current_job = None
        self.runtime_lock = threading.Lock()
        self.run_finished = threading.Event()
        self.run_finished.set()

    def refresh_runtime(self):
        if self.runtime is None or not self.verified:
            self.store.set_runtime("unavailable", "Subscription runtime or verification proof unavailable")
            return self.availability()
        try:
            with self.runtime_lock:
                catalog = self.runtime.models()
            if not isinstance(catalog, list) or not catalog or any(not isinstance(model, dict) or not model.get("id") for model in catalog):
                raise RuntimeFailure("Account catalog unavailable")
            self.catalog = catalog
        except AuthRequired:
            self.store.set_runtime("auth_required", "Subscription authentication must be reconnected locally")
        except QuotaExceeded:
            self.store.set_runtime("paused_quota", "Subscription quota exhausted; queue paused")
        except Exception:
            self.store.set_runtime("unavailable", "Subscription runtime unavailable")
        else:
            if self.store.runtime_state()["state"] not in {"paused_quota", "auth_required"}:
                self.store.set_runtime("ready")
        return self.availability()

    def availability(self):
        state = self.store.runtime_state()
        ready = bool(self.runtime is not None and self.verified and self.catalog and state["state"] == "ready")
        return {"ready": ready, "models": self.catalog, "reason": "" if ready else state["reason"]}

    def resume(self):
        if self.runtime is None or not self.verified:
            raise RuntimeFailure("Subscription runtime unavailable")
        if self.current_job is not None:
            return self.availability()
        # This is an explicit user action; refresh may reveal auth/quota failure again.
        self.store.set_runtime("unavailable", "Checking subscription")
        result = self.refresh_runtime()
        if not result["ready"]:
            raise RuntimeFailure(result["reason"])
        return result

    async def start(self):
        descriptor = os.open(self.store.database.state_dir / "chat-worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return
        self.lock_descriptor = descriptor
        self.store.recover_running()
        if self.controller.notes.pending_count():
            try:
                await asyncio.to_thread(self.controller.apply_note_operations)
            except Exception:
                pass  # Durable intents remain queued when writer state is uncertain.
        await asyncio.to_thread(self.refresh_runtime)
        if self.runtime is not None and self.verified:
            self.task = asyncio.create_task(self._loop())

    async def stop(self):
        self.closing.set()
        if self.current_job:
            self.store.finish(self.current_job, "uncertain", error="Worker stopped; explicit retry is required")
        if self.task:
            try:
                # The runtime observes cancelled(), then reaps its process. Keep
                # worker authority until that thread has actually completed.
                await self.task
            except asyncio.CancelledError:
                self.task.cancel()
                if not self.run_finished.is_set():
                    raise
        if self.lock_descriptor is not None and self.run_finished.is_set():
            os.close(self.lock_descriptor)
            self.lock_descriptor = None

    async def _loop(self):
        while not self.closing.is_set():
            processed = await self.process_next()
            if not processed:
                await asyncio.sleep(0.5)

    async def process_next(self):
        if self.closing.is_set() or self.runtime is None or not self.verified:
            return False
        if self.store.runtime_state()["state"] in {"paused_quota", "auth_required"}:
            return False
        if not self.catalog:
            await asyncio.to_thread(self.refresh_runtime)
        if not self.availability()["ready"]:
            return False
        job = self.store.claim_next()
        if job is None:
            return False
        self.current_job = job["id"]
        self.run_finished.clear()
        try:
            await asyncio.to_thread(self._execute, job)
        finally:
            self.current_job = None
        return True

    def _execute(self, job):
        try:
            self._run(job)
        finally:
            self.run_finished.set()

    def _run(self, job):
        from .notes import NoteTools
        cancelled = lambda: self.closing.is_set() or self.store.cancelled(job["id"])
        fresh_workspace = None
        try:
            if cancelled():
                raise Cancelled()
            if job["model"] is not None and job["model"] not in {model["id"] for model in self.catalog}:
                raise RuntimeFailure("Model is not in the account catalog")
            snapshot = self.controller.notes.snapshot_for_runtime()
            tools = NoteTools(self.controller.notes, self.store, job["id"], snapshot, cancelled)
            messages = self.store.context(job)
            images = []
            runtime = self.runtime
            if hasattr(runtime, "work_dir"):
                workspace = Path(runtime.work_dir) / "turns" / job["id"]
                workspace.mkdir(mode=0o700, parents=True, exist_ok=False)
                fresh_workspace = workspace
                if hasattr(runtime, "auth_dir"):
                    runtime = type(runtime)(runtime.auth_dir, workspace)
                else:
                    # Injectable simulators need the same isolated attachment contract.
                    runtime = copy.copy(runtime)
                    runtime.work_dir = workspace
            if job["attachment_ids"]:
                if self.attachments_resolver is None:
                    raise RuntimeFailure("Attachment resolver unavailable")
                work_dir = Path(runtime.work_dir)
                records = self.attachments_resolver(job["attachment_ids"], work_dir)
                for record in records:
                    if record.get("image_path"):
                        images.append(Path(record["image_path"]))
                    if record.get("text"):
                        messages[-1]["content"] += "\n\n[Attachment: " + record["name"] + "]\n" + record["text"]
                with self.store.database.connect(immediate=True) as connection:
                    connection.execute("UPDATE chat_messages SET attachments=? WHERE id=?", (json.dumps([{key: value for key, value in record.items() if key not in {"text", "image_path"}} for record in records]), job["message_id"]))
            model = job["model"]
            if images:
                if model is None:
                    model = next((item["id"] for item in self.catalog if item.get("supports_images")), None)
                if not any(item["id"] == model and item.get("supports_images") for item in self.catalog):
                    raise RuntimeFailure("Selected account model does not support images")
            with self.runtime_lock:
                answer = runtime.run(messages, model, tools, lambda text: self.store.delta(job["id"], text), cancelled, images=images)
            if cancelled():
                raise Cancelled()
            if not isinstance(answer, str) or not answer.strip() or len(answer) > 1048576:
                raise RuntimeFailure("Subscription returned no usable answer")
            completed = self.store.finish(job["id"], "completed", answer)
            if completed:
                try:
                    self.controller.apply_note_operations()
                except Exception:
                    # Note states remain queued; chat completion never fabricates a write.
                    pass
                if self.completion_hook and job["origin"] == "telegram":
                    self.completion_hook(job, answer)
        except QuotaExceeded:
            self.store.set_runtime("paused_quota", "Subscription quota exhausted; queue paused")
            self.store.finish(job["id"], "failed", error="Subscription quota exhausted")
        except AuthRequired:
            self.store.set_runtime("auth_required", "Subscription authentication must be reconnected locally")
            self.store.finish(job["id"], "failed", error="Subscription authentication required")
        except Cancelled:
            self.store.finish(job["id"], "uncertain" if self.closing.is_set() else "cancelled")
        except RuntimeFailure as error:
            self.store.finish(job["id"], "failed", error=str(error) or "Subscription turn failed; explicit retry is required")
        except Exception:
            self.store.finish(job["id"], "failed", error="Subscription turn failed; explicit retry is required")
        finally:
            if fresh_workspace is not None:
                # The synchronous runtime returns only after its child is reaped.
                shutil.rmtree(fresh_workspace, ignore_errors=True)
