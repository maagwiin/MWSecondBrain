import hashlib

import pytest

from mwsecondbrain.chat import ChatStore
from mwsecondbrain.config import Settings
from mwsecondbrain.controller import Controller, UnsafeOperation
from mwsecondbrain.db import Database
from mwsecondbrain.notes import NotesService, NoteRejected, NoteTools


class Editor:
    available = True
    state = "stopped"
    def status(self): return self.state
    def start(self): self.state = "running"
    def stop(self): self.state = "stopped"


@pytest.fixture
def context(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = Settings(vault, tmp_path / "state", tmp_path / "backups", "https://brain.example.test")
    database = Database(settings.state_dir)
    syncs = []
    controller = Controller(settings, database, Editor(), lambda _: syncs.append("sync") or {"state": "pending", "pending": True}, lambda *args: {"state": "success"})
    service = NotesService(controller, policy=lambda path, data: "Secret detected" if b"SECRET" in data else None)
    controller.notes = service
    store = ChatStore(database)
    job = store.enqueue("capture", "web", "one")
    store.claim_next()
    return controller, service, store, job, syncs


def proposal(context, path, content, base_hash=None):
    controller, service, store, job, _ = context
    snapshot = service.capture_snapshot()
    result = service.propose(job["job_id"], path, content, base_hash, "capture", snapshot)
    store.finish(job["job_id"], "completed", "answer")
    return result


@pytest.mark.parametrize("path", ["../outside.md", "/tmp/outside.md", ".git/config.md", "notes/../../escape.md", "AGENTS.md", "notes/CLAUDE.md", "script.py", "notes\\outside.md", "notes//double.md"])
def test_note_paths_and_instructions_are_rejected(context, path):
    with pytest.raises(NoteRejected): proposal(context, path, "content")


def test_secret_scanner_blocks_proposal_and_full_preflight(context):
    controller, service, _, _, _ = context
    with pytest.raises(NoteRejected, match="Secret"):
        proposal(context, "note.md", "SECRET credential")
    proposal(context, "note.md", "safe text")
    (controller.settings.vault / "other.md").write_text("SECRET leaked elsewhere")
    result = controller.apply_note_operations()
    assert result["rejected"] == 1
    assert not (controller.settings.vault / "note.md").exists()


def test_safe_addition_is_applied_once_and_syncs_immediately(context):
    controller, service, store, _, syncs = context
    operation = proposal(context, "notes/new.md", "safe capture")
    assert controller.apply_note_operations()["applied"] == 1
    assert (controller.settings.vault / "notes/new.md").read_text() == "safe capture"
    assert syncs == ["sync"]
    assert controller.apply_note_operations()["applied"] == 0
    assert store.view()["operations"][0]["id"] == operation["operation_id"]
    assert store.view()["operations"][0]["status"] == "applied"


def test_stale_original_hash_never_overwrites_human_changes(context):
    controller, service, _, _, _ = context
    path = controller.settings.vault / "note.md"
    path.write_text("original")
    base = hashlib.sha256(b"original").hexdigest()
    proposal(context, "note.md", "generated", base)
    path.write_text("human changed this")
    assert controller.apply_note_operations()["rejected"] == 1
    assert path.read_text() == "human changed this"


def test_snapshot_while_editing_reads_original_and_proposals_wait(context):
    controller, service, store, job, syncs = context
    path = controller.settings.vault / "note.md"
    path.write_text("original")
    controller.change_mode("editing")
    snapshot = service.snapshot_for_runtime()
    path.write_text("editor changed")
    tools = NoteTools(service, store, job["job_id"], snapshot)
    assert tools("brain_read", {"path": "note.md"})["content"] == "original"
    tools("brain_propose_update", {"path": "new.md", "content": "capture", "base_hash": None, "reason": "capture"})
    store.finish(job["job_id"], "completed", "answer")
    assert controller.apply_note_operations()["queued"] == 1
    assert not (controller.settings.vault / "new.md").exists()
    controller.change_mode("agent")
    assert (controller.settings.vault / "new.md").read_text() == "capture"


def test_symlink_directory_never_writes_outside_vault(context, tmp_path):
    controller, service, _, _, _ = context
    proposal(context, "link/note.md", "safe")
    outside = tmp_path / "outside"
    outside.mkdir()
    (controller.settings.vault / "link").symlink_to(outside, target_is_directory=True)
    assert controller.apply_note_operations()["rejected"] == 1
    assert not (outside / "note.md").exists()


def test_crash_after_write_recognizes_already_applied_hash(context):
    controller, service, store, _, syncs = context
    operation = proposal(context, "note.md", "captured")
    (controller.settings.vault / "note.md").write_text("captured")
    with controller.database.connect() as connection:
        connection.execute("UPDATE note_operations SET status='applying' WHERE id=?", (operation["operation_id"],))
    assert controller.apply_note_operations()["applied"] == 1
    assert store.view()["operations"][0]["status"] == "applied"
    assert (controller.settings.vault / "note.md").read_text() == "captured"
    assert syncs == ["sync"]


def test_scanner_unavailable_refuses_writes(context):
    controller, service, _, job, _ = context
    service.policy = None
    with pytest.raises(NoteRejected, match="scanner"):
        service.propose(job["job_id"], "note.md", "safe", None, "capture", service.capture_snapshot())
    assert not (controller.settings.vault / "note.md").exists()


def test_note_operation_obeys_controller_lock_and_unknown_writer_state(context):
    controller, service, _, _, _ = context
    proposal(context, "note.md", "safe")
    with controller.exclusive():
        with pytest.raises(UnsafeOperation): controller.apply_note_operations()
    controller.editor.state = "unknown"
    with pytest.raises(UnsafeOperation): controller.apply_note_operations()
    assert not (controller.settings.vault / "note.md").exists()


def test_pausing_capture_before_apply_rejects_pending_proposals(context):
    controller, service, store, _, _ = context
    proposal(context, "note.md", "safe")
    store.set_capture(True)
    assert controller.apply_note_operations()["rejected"] == 1
    assert not (controller.settings.vault / "note.md").exists()


def test_search_and_read_exclude_secrets_and_instruction_files(context):
    controller, service, store, job, _ = context
    (controller.settings.vault / "ordinary.md").write_text("Useful memory")
    (controller.settings.vault / "secret.md").write_text("SECRET must stay private")
    (controller.settings.vault / "AGENTS.md").write_text("Trusted instructions")
    snapshot = service.capture_snapshot()
    tools = NoteTools(service, store, job["job_id"], snapshot)
    assert tools("brain_search", {"query": "memory"})["matches"][0]["path"] == "ordinary.md"
    assert tools("brain_read", {"path": "secret.md"})["exists"] is False
    assert tools("brain_read", {"path": "AGENTS.md"}).get("error")
    assert tools("shell", {"command": "cat /etc/passwd"}).get("error")


def test_unreadable_snapshot_does_not_block_phase_one_editor(context, monkeypatch):
    controller, service, _, _, _ = context
    def inaccessible():
        raise PermissionError("Cannot read a note")
        yield
    monkeypatch.setattr(service, "_files", inaccessible)
    assert controller.change_mode("editing")["mode"] == "editing"


def test_scheduler_drains_proposals_after_lock_contention(context):
    from mwsecondbrain.app import scheduler_tick
    controller, service, store, _, _ = context
    proposal(context, "note.md", "capture")
    with controller.exclusive():
        with pytest.raises(UnsafeOperation): controller.apply_note_operations()
    scheduler_tick(controller, 1_800_000_000)
    assert (controller.settings.vault / "note.md").read_text() == "capture"
    assert store.view()["operations"][0]["status"] == "applied"


def test_startup_drains_completed_note_intents_without_replaying_inference(context):
    import asyncio
    from mwsecondbrain.chat import ChatWorker
    controller, service, store, _, _ = context
    proposal(context, "note.md", "capture")
    worker = ChatWorker(store, controller)
    async def scenario():
        await worker.start()
        await worker.stop()
    asyncio.run(scenario())
    assert (controller.settings.vault / "note.md").read_text() == "capture"
    assert store.view()["operations"][0]["status"] == "applied"
