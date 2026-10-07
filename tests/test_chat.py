import asyncio
import threading

import pytest
from fastapi.testclient import TestClient

from mwsecondbrain.app import create_app
from mwsecondbrain.chat import ChatStore, ChatWorker, QuotaExceeded, AuthRequired, Cancelled
from mwsecondbrain.config import Settings
from mwsecondbrain.controller import Controller, UnsafeOperation
from mwsecondbrain.db import Database


class Editor:
    available = True
    state = "stopped"
    def status(self): return self.state
    def start(self): self.state = "running"
    def stop(self): self.state = "stopped"


class Runtime:
    def __init__(self):
        self.calls = []
    def models(self):
        return [{"id": "account-model", "label": "Account model", "supports_images": False}]
    def run(self, messages, model, tools, on_delta, cancelled, images=None):
        self.calls.append(messages)
        on_delta("Olá ")
        on_delta("mundo")
        return "Olá mundo"


@pytest.fixture
def context(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = Settings(vault, tmp_path / "state", tmp_path / "backups", "https://brain.example.test")
    database = Database(settings.state_dir)
    controller = Controller(settings, database, Editor(), lambda _: {"state": "pending", "pending": True}, lambda *args: {"state": "success"})
    return settings, database, controller


def test_queue_deduplication_preserves_origin_and_order(context):
    _, database, _ = context
    store = ChatStore(database)
    first = store.enqueue("web", "web", "same")
    assert store.enqueue("changed retry", "web", "same") == first
    second = store.enqueue("telegram", "telegram", "same")
    assert second != first
    assert [job["origin"] for job in store.view()["jobs"]] == ["web", "telegram"]


def test_worker_persists_streamed_answer_and_context(context):
    _, database, controller = context
    store = ChatStore(database)
    runtime = Runtime()
    store.enqueue("primeira", "web", "1")
    worker = ChatWorker(store, controller, runtime)
    asyncio.run(worker.process_next())
    view = store.view()
    assert [message["content"] for message in view["messages"]] == ["primeira", "Olá mundo"]
    assert all(message["status"] == "completed" for message in view["messages"])
    events = store.events_after(0)
    assert [event["delta"] for event in events if event["type"] == "delta"] == ["Olá ", "mundo"]
    store.enqueue("segunda", "telegram", "2")
    asyncio.run(worker.process_next())
    assert [message["content"] for message in runtime.calls[-1]] == ["primeira", "Olá mundo", "segunda"]


def test_queued_cancellation_does_not_call_runtime(context):
    _, database, controller = context
    store = ChatStore(database)
    job = store.enqueue("cancelar", "web", "1")
    assert store.cancel(job["job_id"]) is True
    runtime = Runtime()
    assert asyncio.run(ChatWorker(store, controller, runtime).process_next()) is False
    assert runtime.calls == []
    assert store.view()["jobs"][0]["status"] == "cancelled"


def test_running_cancel_is_persistent_and_keeps_event_loop_free(context):
    _, database, controller = context
    store = ChatStore(database)
    job = store.enqueue("cancelar", "web", "1")
    entered = threading.Event()
    class BlockingRuntime(Runtime):
        def run(self, messages, model, tools, on_delta, cancelled, images=None):
            entered.set()
            while not cancelled():
                threading.Event().wait(0.01)
            raise Cancelled()
    async def scenario():
        task = asyncio.create_task(ChatWorker(store, controller, BlockingRuntime()).process_next())
        for _ in range(200):
            if entered.is_set(): break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        store.cancel(job["job_id"])
        await asyncio.wait_for(task, 2)
    asyncio.run(scenario())
    assert store.view()["jobs"][0]["status"] == "cancelled"


@pytest.mark.parametrize("cancel_waiting", [False, True])
def test_controller_contention_waits_before_inference_and_honors_cancel(context, monkeypatch, cancel_waiting):
    _, database, controller = context
    store, runtime = ChatStore(database), Runtime()
    job = store.enqueue("wait for controller", "web", "controller-wait")
    worker = ChatWorker(store, controller, runtime)
    attempted = threading.Event()
    snapshot = controller.notes.snapshot_for_runtime

    def observe_snapshot():
        attempted.set()
        return snapshot()

    monkeypatch.setattr(controller.notes, "snapshot_for_runtime", observe_snapshot)

    async def scenario():
        with controller.exclusive():
            task = asyncio.create_task(worker.process_next())
            for _ in range(200):
                if attempted.is_set():
                    break
                await asyncio.sleep(0.01)
            assert attempted.is_set()
            assert runtime.calls == []
            assert store.view()["jobs"][0]["status"] == "running"
            if cancel_waiting:
                store.cancel(job["job_id"])
                await asyncio.wait_for(task, 2)
        if not cancel_waiting:
            await asyncio.wait_for(task, 2)

    asyncio.run(scenario())
    assert len(runtime.calls) == (0 if cancel_waiting else 1)
    assert store.view()["jobs"][0]["status"] == ("cancelled" if cancel_waiting else "completed")


def test_nonbusy_snapshot_failure_is_not_retried(context, monkeypatch):
    _, database, controller = context
    store, runtime = ChatStore(database), Runtime()
    store.enqueue("unsafe snapshot", "web", "snapshot-error")
    attempts = []

    def fail_snapshot():
        attempts.append(True)
        raise UnsafeOperation("Editor is running or its writer state is unknown")

    monkeypatch.setattr(controller.notes, "snapshot_for_runtime", fail_snapshot)
    asyncio.run(ChatWorker(store, controller, runtime).process_next())
    assert len(attempts) == 1
    assert runtime.calls == []
    assert store.view()["jobs"][0]["status"] == "failed"


def test_controller_snapshot_wait_has_finite_deadline(context, monkeypatch):
    from types import SimpleNamespace
    import mwsecondbrain.chat as chat_module
    from mwsecondbrain.controller import ControllerBusy

    _, database, controller = context
    store, runtime = ChatStore(database), Runtime()
    store.enqueue("bounded controller wait", "web", "bounded-wait")
    times = iter([0, 301])
    monkeypatch.setattr(chat_module, "time", SimpleNamespace(monotonic=lambda: next(times), sleep=lambda _: None))

    def busy_snapshot():
        raise ControllerBusy("Another controller operation is running")

    monkeypatch.setattr(controller.notes, "snapshot_for_runtime", busy_snapshot)
    asyncio.run(ChatWorker(store, controller, runtime).process_next())
    job = store.view()["jobs"][0]
    assert job["status"] == "failed"
    assert job["error"] == "Controller stayed busy before inference; explicit retry is required"
    assert runtime.calls == []


def test_restart_marks_running_job_uncertain_without_replay(context):
    _, database, controller = context
    store = ChatStore(database)
    store.enqueue("não repetir", "web", "1")
    assert store.claim_next()["status"] == "running"
    restarted = ChatStore(Database(database.state_dir))
    restarted.recover_running()
    runtime = Runtime()
    assert asyncio.run(ChatWorker(restarted, controller, runtime).process_next()) is False
    assert restarted.view()["jobs"][0]["status"] == "uncertain"
    assert runtime.calls == []


@pytest.mark.parametrize("failure,state", [(QuotaExceeded, "paused_quota"), (AuthRequired, "auth_required")])
def test_runtime_failure_pauses_remaining_queue_until_explicit_resume(context, failure, state):
    _, database, controller = context
    store = ChatStore(database)
    store.enqueue("primeira", "web", "1")
    store.enqueue("segunda", "telegram", "2")
    class FailingRuntime(Runtime):
        def run(self, *args, **kwargs): raise failure()
    worker = ChatWorker(store, controller, FailingRuntime())
    asyncio.run(worker.process_next())
    assert store.runtime_state()["state"] == state
    assert asyncio.run(worker.process_next()) is False
    assert [job["status"] for job in store.view()["jobs"]] == ["failed", "queued"]
    worker.runtime = Runtime()
    worker.resume()
    assert asyncio.run(worker.process_next()) is True


def test_new_conversation_cancels_old_queue_and_preserves_history(context):
    _, database, _ = context
    store = ChatStore(database)
    old = store.view()["conversation_id"]
    store.enqueue("old", "web", "1")
    new = store.new_conversation()
    assert new != old
    assert store.view()["messages"] == []
    assert store.view(old)["jobs"][0]["status"] == "cancelled"
    assert len(store.conversations()) == 2


def test_enqueue_rejects_stale_conversation_without_insert(context):
    _, database, _ = context
    store = ChatStore(database)
    old = store.view()["conversation_id"]
    new = store.new_conversation()
    with pytest.raises(ValueError, match="Conversation is no longer active"):
        store.enqueue("stale tab", "web", "stale", conversation_id=old)
    assert store.view(new)["messages"] == []
    assert store.view(old)["messages"] == []
    assert store.events_after(0)[-1]["type"] == "conversation"
    accepted = store.enqueue("current tab", "web", "current", conversation_id=new)
    assert store.enqueue("idempotent", "web", "current", conversation_id=new) == accepted
    store.enqueue("telegram active", "telegram", "telegram")
    assert [message["content"] for message in store.view(new)["messages"]] == ["current tab", "telegram active"]


def test_chat_http_rejects_stale_conversation_after_new(context):
    settings, _, _ = context
    app = create_app(settings, editor=Editor(), runtime=Runtime(), runtime_verified=True, scheduler_enabled=False, chat_worker_enabled=False)
    app.state.auth.set_password("strong local password")
    with TestClient(app, base_url=settings.public_origin) as browser:
        login = browser.post("/api/login", json={"password": "strong local password"}, headers={"Origin": settings.public_origin})
        headers = {"Origin": settings.public_origin, "X-CSRF-Token": login.json()["csrf_token"]}
        old = browser.get("/api/chat").json()["conversation_id"]
        new = browser.post("/api/chat/new", json={}, headers=headers).json()["conversation_id"]
        rejected = browser.post("/api/chat/messages", json={"text": "stale", "idempotency_key": "stale", "conversation_id": old}, headers=headers)
        assert rejected.status_code == 400
        assert rejected.json()["detail"] == "Invalid chat request"
        assert browser.get("/api/chat").json()["messages"] == []
        accepted = browser.post("/api/chat/messages", json={"text": "current", "idempotency_key": "current", "conversation_id": new}, headers=headers)
        assert accepted.status_code == 202


def test_outbox_claim_is_durable_and_never_blindly_replayed(context):
    _, database, controller = context
    store = ChatStore(database)
    store.enqueue("telegram", "telegram", "update-9", external_reply={"chat_id": 42})
    asyncio.run(ChatWorker(store, controller, Runtime()).process_next())
    outbox = store.pending_outbox()
    assert len(outbox) == 1
    assert outbox[0]["content"] == "Olá mundo"
    assert store.claim_outbox(outbox[0]["id"]) is True
    assert store.claim_outbox(outbox[0]["id"]) is False
    restarted = ChatStore(Database(database.state_dir))
    assert restarted.pending_outbox() == []
    assert restarted.view()["jobs"][0]["delivery_status"] == "uncertain"
    restarted.mark_outbox_sent(outbox[0]["id"])
    assert restarted.view()["jobs"][0]["delivery_status"] == "sent"


def test_event_ids_replay_increase_across_restarts(context):
    _, database, _ = context
    store = ChatStore(database)
    store.enqueue("a", "web", "1")
    last = store.events_after(0)[-1]["id"]
    restarted = ChatStore(Database(database.state_dir))
    restarted.enqueue("b", "web", "2")
    events = restarted.events_after(last)
    assert events and all(event["id"] > last for event in events)


def test_capture_pause_and_explicit_opt_out_deny_proposals(context):
    _, database, controller = context
    store = ChatStore(database)
    outputs = []
    class CaptureRuntime(Runtime):
        def run(self, messages, model, tools, on_delta, cancelled, images=None):
            outputs.append(tools("brain_propose_update", {"path": "note.md", "content": "safe", "base_hash": None, "reason": "capture"}))
            return "ok"
    store.set_capture(True)
    store.enqueue("guarde", "web", "1")
    store.set_capture(False)
    worker = ChatWorker(store, controller, CaptureRuntime())
    asyncio.run(worker.process_next())
    store.enqueue("Não guarde esta mensagem", "web", "2")
    asyncio.run(worker.process_next())
    store.enqueue("pedido sem captura", "web", "3", no_capture=True)
    asyncio.run(worker.process_next())
    assert len(outputs) == 3
    assert all(output.get("error") for output in outputs)
    assert all(job["capture_denied"] is True for job in store.view()["jobs"])
    assert not (controller.settings.vault / "note.md").exists()
    assert store.view()["operations"] == []


def test_capture_opt_out_survives_restart_and_idempotent_retry(context):
    _, database, _ = context
    store = ChatStore(database)
    original = store.enqueue("privado", "web", "private", no_capture=True)
    restarted = ChatStore(Database(database.state_dir))
    assert restarted.enqueue("retry", "web", "private", no_capture=False) == original
    assert restarted.view()["jobs"][0]["capture_denied"] is True
    assert restarted.claim_next()["capture_denied"] == 1


def test_capture_pause_is_latched_at_enqueue_even_with_false_opt_out(context):
    _, database, _ = context
    store = ChatStore(database)
    store.set_capture(True)
    store.enqueue("privado", "telegram", "paused", no_capture=False)
    store.set_capture(False)
    assert store.view()["capture_paused"] is False
    assert store.view()["jobs"][0]["capture_denied"] is True


def test_chat_http_requires_auth_csrf_and_verified_runtime(context):
    settings, _, _ = context
    app = create_app(settings, editor=Editor(), runtime=Runtime(), runtime_verified=True, scheduler_enabled=False, chat_worker_enabled=False)
    app.state.auth.set_password("strong local password")
    with TestClient(app, base_url=settings.public_origin) as browser:
        assert browser.get("/api/chat").status_code == 401
        assert browser.get("/api/chat/events").status_code == 401
        response = browser.post("/api/login", json={"password": "strong local password"}, headers={"Origin": settings.public_origin})
        csrf = response.json()["csrf_token"]
        assert browser.get("/api/models").json()["models"][0]["id"] == "account-model"
        assert browser.get("/api/status").json()["phase2"]["ready"] is True
        assert browser.post("/api/chat/messages", json={"text": "hello", "idempotency_key": "1"}).status_code == 403
        headers = {"Origin": settings.public_origin, "X-CSRF-Token": csrf}
        response = browser.post("/api/chat/messages", json={"text": "hello", "idempotency_key": "1", "no_capture": True}, headers=headers)
        assert response.status_code == 202
        assert browser.get("/api/chat").json()["messages"][0]["content"] == "hello"
        assert browser.get("/api/chat").json()["jobs"][0]["capture_denied"] is True
        assert browser.post("/api/chat/messages", json={"text": "bad", "idempotency_key": "bad", "no_capture": "false"}, headers=headers).status_code == 422


def test_phase_one_remains_available_without_optional_runtime(context):
    settings, _, _ = context
    app = create_app(settings, editor=Editor(), scheduler_enabled=False, chat_worker_enabled=False)
    assert app.state.chat_worker.availability()["ready"] is False
    with TestClient(app, base_url=settings.public_origin) as browser:
        assert browser.get("/healthz").json() == {"status": "ok"}


def test_mode_response_preserves_ready_subscription_runtime(context, monkeypatch):
    settings, _, _ = context
    monkeypatch.setenv("MWSB_TELEGRAM_CONFIG", str(settings.state_dir / "disabled-telegram.json"))
    app = create_app(settings, editor=Editor(), runtime=Runtime(), runtime_verified=True,
                     sync_callback=lambda _: {"state": "success", "pending": False},
                     backup_callback=lambda *args: {"state": "success"},
                     scheduler_enabled=False, chat_worker_enabled=False)
    app.state.auth.set_password("strong local password")
    with TestClient(app, base_url=settings.public_origin) as browser:
        login = browser.post("/api/login", json={"password": "strong local password"}, headers={"Origin": settings.public_origin})
        headers = {"Origin": settings.public_origin, "X-CSRF-Token": login.json()["csrf_token"]}
        assert browser.get("/api/status").json()["phase2"] == {"ready": True, "reason": ""}
        for target in ("editing", "agent"):
            changed = browser.post("/api/mode", json={"mode": target}, headers=headers)
            assert changed.status_code == 200
            assert changed.json()["mode"] == target
            assert changed.json()["phase2"] == browser.get("/api/status").json()["phase2"] == {"ready": True, "reason": ""}


def test_queued_turns_are_paired_in_model_context(context):
    _, database, controller = context
    store, runtime = ChatStore(database), Runtime()
    for index in range(3): store.enqueue(f"turn {index}", "web", str(index))
    worker = ChatWorker(store, controller, runtime)
    for _ in range(3): asyncio.run(worker.process_next())
    assert [message["content"] for message in runtime.calls[-1]] == ["turn 0", "Olá mundo", "turn 1", "Olá mundo", "turn 2"]


def test_non_catalog_model_fails_without_inference(context):
    _, database, controller = context
    store, runtime = ChatStore(database), Runtime()
    store.enqueue("no paid fallback", "web", "1", model="invented-model")
    asyncio.run(ChatWorker(store, controller, runtime).process_next())
    assert runtime.calls == []
    assert store.view()["jobs"][0]["status"] == "failed"


def test_attachment_text_is_included_and_paths_are_not_exposed(context, tmp_path):
    _, database, controller = context
    store, runtime = ChatStore(database), Runtime()
    runtime.work_dir = tmp_path / "runtime"
    store.enqueue("summarize", "web", "1", attachment_ids=["document"])
    def resolve(ids, work_dir):
        assert ids == ["document"]
        assert work_dir.parent == runtime.work_dir / "turns"
        return [{"id": "document", "name": "test.pdf", "mime": "application/pdf", "text": "Extracted document", "partial": True, "message": "Partial extraction"}]
    asyncio.run(ChatWorker(store, controller, runtime, attachments_resolver=resolve).process_next())
    assert "Extracted document" in runtime.calls[0][-1]["content"]
    metadata = store.view()["messages"][0]["attachments"][0]
    assert metadata["partial"] is True
    assert "text" not in metadata
    assert "image_path" not in metadata


def test_explicit_runtime_gate_is_required(context):
    _, database, controller = context
    store, runtime = ChatStore(database), Runtime()
    store.enqueue("not verified", "web", "1")
    worker = ChatWorker(store, controller, runtime, verified=False)
    assert asyncio.run(worker.process_next()) is False
    assert worker.availability()["ready"] is False
    assert runtime.calls == []


def test_sse_stops_replay_when_session_is_revoked(context):
    from starlette.requests import Request
    settings, _, _ = context
    app = create_app(settings, editor=Editor(), scheduler_enabled=False, chat_worker_enabled=False)
    app.state.auth.set_password("strong local password")
    token, _ = app.state.auth.login("strong local password", "127.0.0.1")
    app.state.chat_store.enqueue("queued", "web", "sse")
    async def waiting_receive():
        await asyncio.Event().wait()
    request = Request({"type": "http", "method": "GET", "path": "/api/chat/events", "headers": [(b"cookie", f"mwsb_session={token}".encode())], "query_string": b"", "scheme": "https", "server": ("brain.example.test", 443)}, waiting_receive)
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/chat/events")
    async def scenario():
        response = await endpoint(request, current={})
        first = await anext(response.body_iterator)
        assert "id:" in first and "data:" in first
        app.state.auth.logout(token)
        with pytest.raises(StopAsyncIteration):
            await anext(response.body_iterator)
    asyncio.run(scenario())


def test_worker_shutdown_waits_for_runtime_reaping_and_marks_uncertain(context):
    _, database, controller = context
    store = ChatStore(database)
    entered, reaped = threading.Event(), threading.Event()
    class ShutdownRuntime(Runtime):
        def run(self, messages, model, tools, on_delta, cancelled, images=None):
            entered.set()
            while not cancelled(): threading.Event().wait(0.01)
            reaped.set()
            raise Cancelled()
    store.enqueue("running", "web", "1")
    worker = ChatWorker(store, controller, ShutdownRuntime())
    async def scenario():
        await worker.start()
        for _ in range(200):
            if entered.is_set(): break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        await worker.stop()
        assert reaped.is_set()
        assert worker.lock_descriptor is None
    asyncio.run(scenario())
    assert store.view()["jobs"][0]["status"] == "uncertain"


def test_worker_attaches_snapshot_guidance_without_promoting_ordinary_notes(context):
    from mwsecondbrain.notes import NotesService
    _, database, controller = context
    controller.notes = NotesService(controller, policy=lambda path, content: None)
    (controller.settings.vault / "AGENTS.md").write_text("Read-only brain instructions")
    (controller.settings.vault / "ordinary.md").write_text("Ordinary untrusted note")
    seen = []
    class GuidanceRuntime(Runtime):
        def run(self, *args, **kwargs):
            seen.append(self.brain_guidance)
            return "ok"
    store = ChatStore(database)
    store.enqueue("hello", "web", "guidance")
    asyncio.run(ChatWorker(store, controller, GuidanceRuntime()).process_next())
    assert "Read-only brain instructions" in seen[0]
    assert "Ordinary untrusted note" not in seen[0]
