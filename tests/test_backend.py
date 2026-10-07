import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mwsecondbrain.app import create_app
from mwsecondbrain.auth import Auth
from mwsecondbrain.config import Settings
from mwsecondbrain.controller import Controller, UnsafeOperation
from mwsecondbrain.db import Database


class Editor:
    available = True

    def __init__(self, state="stopped"):
        self.state = state
        self.calls = []

    def status(self):
        self.calls.append("status")
        return self.state

    def start(self):
        self.calls.append("start")
        self.state = "running"

    def stop(self):
        self.calls.append("stop")
        self.state = "stopped"


@pytest.fixture
def settings(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    return Settings(vault=vault, state_dir=tmp_path / "state", backup_dir=tmp_path / "backups", public_origin="https://brain.example.test")


@pytest.fixture
def client(settings):
    clock = [1_800_000_000.0]
    app = create_app(settings, editor=Editor(), sync_callback=lambda _: {"state": "synced", "message": "ok", "pending": False}, backup_callback=lambda *args: {"state": "ok"}, clock=lambda: clock[0], scheduler_enabled=False)
    app.state.auth.set_password("a strong initial password")
    with TestClient(app, base_url=settings.public_origin) as browser:
        yield browser, app, clock


def login(browser):
    response = browser.post("/api/login", json={"password": "a strong initial password"}, headers={"Origin": "https://brain.example.test"})
    assert response.status_code == 200
    return response.json()["csrf_token"]


def mutation(browser, csrf):
    return {"Origin": "https://brain.example.test", "X-CSRF-Token": csrf}


def test_telegram_config_does_not_enable_connector(settings, monkeypatch, tmp_path):
    from mwsecondbrain import telegram

    config = tmp_path / "telegram.json"
    config.write_text("{}")
    monkeypatch.setenv("MWSB_TELEGRAM_CONFIG", str(config))
    monkeypatch.delenv("MWSB_TELEGRAM_ENABLED", raising=False)

    def unexpected_connector(*args, **kwargs):
        pytest.fail("Telegram must require explicit enablement")

    monkeypatch.setattr(telegram, "TelegramService", unexpected_connector)
    app = create_app(settings, editor=Editor(), scheduler_enabled=False,
                     chat_worker_enabled=False)
    with TestClient(app):
        assert app.state.telegram is None


def test_auth_cookie_session_and_revocation(client):
    browser, app, _ = client
    assert browser.get("/api/session").status_code == 401
    csrf = login(browser)
    cookie = browser.cookies.get("mwsb_session")
    assert cookie
    response = browser.post("/api/login", json={"password": "a strong initial password"}, headers={"Origin": app.state.settings.public_origin})
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    csrf = response.json()["csrf_token"]
    assert browser.get("/api/session").json()["csrf_token"] == csrf
    assert browser.post("/api/logout", headers=mutation(browser, csrf)).status_code == 200
    assert browser.get("/api/session").status_code == 401


def test_invalid_login_and_bounded_ip_throttle(client):
    browser, app, _ = client
    for _ in range(5):
        assert browser.post("/api/login", json={"password": "wrong"}, headers={"Origin": app.state.settings.public_origin}).status_code == 401
    assert browser.post("/api/login", json={"password": "a strong initial password"}, headers={"Origin": app.state.settings.public_origin}).status_code == 429


def test_idle_and_absolute_expiry(client):
    browser, _, clock = client
    login(browser)
    clock[0] += 12 * 3600
    assert browser.get("/api/session").status_code == 401
    login(browser)
    for _ in range(14):
        clock[0] += 11 * 3600
        assert browser.get("/api/session").status_code == 200
    clock[0] += 10 * 3600
    assert browser.get("/api/session").status_code == 200
    clock[0] += 4 * 3600
    assert browser.get("/api/session").status_code == 401


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://evil.test"}, {"Origin": "null"}])
def test_login_requires_public_origin(client, headers):
    browser, _, _ = client
    assert browser.post("/api/login", json={"password": "a strong initial password"}, headers=headers).status_code == 403


def test_mutations_require_origin_and_csrf(client):
    browser, _, _ = client
    csrf = login(browser)
    assert browser.post("/api/sync").status_code == 403
    assert browser.post("/api/sync", headers={"Origin": "https://brain.example.test"}).status_code == 403
    assert browser.post("/api/sync", headers={"Origin": "https://evil.test", "X-CSRF-Token": csrf}).status_code == 403
    assert browser.post("/api/sync", headers=mutation(browser, csrf)).status_code == 200


def test_proxy_auth_only_in_confirmed_editing_mode(client):
    browser, app, _ = client
    assert browser.get("/api/proxy-auth").status_code == 401
    csrf = login(browser)
    assert browser.get("/api/proxy-auth").status_code == 403
    assert browser.post("/api/mode", json={"mode": "editing"}, headers=mutation(browser, csrf)).status_code == 200
    assert browser.get("/api/proxy-auth").status_code == 200
    assert browser.post("/api/sync", headers=mutation(browser, csrf)).status_code == 409
    app.state.controller.editor.state = "unknown"
    assert browser.get("/api/proxy-auth").status_code == 403


@pytest.mark.parametrize("state", ["running", "unknown"])
def test_controller_never_writes_with_unconfirmed_editor(settings, state):
    calls = []
    controller = Controller(settings, Database(settings.state_dir), Editor(state), lambda _: calls.append("sync"), lambda *args: calls.append("backup"))
    with pytest.raises(UnsafeOperation):
        controller.sync()
    with pytest.raises(UnsafeOperation):
        controller.backup()
    assert calls == []
    assert controller.status()["mode"] == "error"


def test_controller_restart_while_editing_recovers_fail_closed(settings):
    editor = Editor()
    database = Database(settings.state_dir)
    first = Controller(settings, database, editor, lambda _: {"state": "synced", "pending": False}, lambda *args: {"state": "success"})
    first.change_mode("editing")
    assert first.status()["mode"] == "editing"
    second = Controller(settings, Database(settings.state_dir), editor, lambda _: {"state": "synced", "pending": False}, lambda *args: {"state": "success"})
    assert second.status()["mode"] == "error"
    assert editor.state == "stopped"
    second.change_mode("agent")
    assert second.status()["mode"] == "agent"


def test_controller_lock_is_shared_between_instances(settings):
    first = Controller(settings, Database(settings.state_dir), Editor(), lambda _: {}, lambda *args: {})
    second = Controller(settings, Database(settings.state_dir), Editor(), lambda _: {}, lambda *args: {})
    with first.exclusive():
        with pytest.raises(UnsafeOperation):
            second.sync()


def test_transition_stops_editor_before_sync(settings):
    editor = Editor()
    events = editor.calls
    def sync(_):
        events.append("sync")
        assert editor.state == "stopped"
        return {"state": "synced", "pending": False}
    controller = Controller(settings, Database(settings.state_dir), editor, sync, lambda *args: {"state": "success"})
    controller.change_mode("editing")
    assert events.index("sync") < events.index("start")
    events.clear()
    controller.change_mode("agent")
    assert events.index("stop") < events.index("sync")
    assert controller.status()["mode"] == "agent"


def test_missing_production_adapters_fail_visibly(settings, monkeypatch):
    def unavailable(*args):
        raise ImportError("Adapter deliberately unavailable in this test")
    monkeypatch.setattr("mwsecondbrain.controller.importlib.import_module", unavailable)
    controller = Controller(settings, Database(settings.state_dir), Editor())
    with pytest.raises(UnsafeOperation, match="sync"):
        controller.sync()
    with pytest.raises(UnsafeOperation, match="backup"):
        controller.backup()


def test_unconfigured_scanner_remains_visible_in_controller_status(settings):
    controller = Controller(settings, Database(settings.state_dir), Editor(), lambda _: {"state": "NOT_CONFIGURED", "message": "Trusted scanner configuration missing", "pending": True}, lambda *args: {"state": "success"})
    result = controller.sync()
    assert result["state"] == "NOT_CONFIGURED"
    status = controller.status()["sync"]
    assert status["state"] == "NOT_CONFIGURED"
    assert status["message"] == "Trusted scanner configuration missing"
    assert status["last_synced_at"] is None


def test_phase2_is_unavailable_and_health_has_no_private_data(client):
    browser, _, _ = client
    assert browser.get("/healthz").json() == {"status": "ok"}
    login(browser)
    status = browser.get("/api/status").json()
    assert status["phase2"]["ready"] is False
    assert "vault" not in status


def test_password_hash_reset_revokes_sessions(client):
    browser, app, _ = client
    login(browser)
    token = browser.cookies.get("mwsb_session")
    with app.state.database.connect() as connection:
        hashed = connection.execute("SELECT value FROM metadata WHERE key='password_hash'").fetchone()[0]
        stored = connection.execute("SELECT token_hash FROM sessions").fetchone()[0]
    assert hashed.startswith("$argon2id$")
    assert stored != token
    app.state.auth.set_password("another strong password")
    assert browser.get("/api/session").status_code == 401


def test_throttle_window_expiry_and_forwarded_header_cannot_bypass(client):
    browser, app, clock = client
    for attempt in range(5):
        response = browser.post("/api/login", json={"password": "wrong"}, headers={"Origin": app.state.settings.public_origin, "X-Forwarded-For": f"192.0.2.{attempt}"})
        assert response.status_code == 401
    assert browser.post("/api/login", json={"password": "wrong"}, headers={"Origin": app.state.settings.public_origin, "X-Forwarded-For": "198.51.100.10"}).status_code == 429
    clock[0] += 15 * 60
    login(browser)


def test_login_body_limit_content_length_and_chunked(client):
    browser, _, _ = client
    headers = {"Origin": "https://brain.example.test"}
    assert browser.post("/api/login", content=b"x" * 4097, headers=headers).status_code == 413
    assert browser.post("/api/login", content=iter([b"x" * 2000, b"x" * 2097]), headers=headers).status_code == 413


def test_partial_editor_start_failure_blocks_all_writes(settings):
    class PartialStart(Editor):
        def start(self):
            self.state = "running"
            raise RuntimeError("Container is running but startup timed out")
    calls = []
    editor = PartialStart()
    def sync(_):
        calls.append("sync")
        return {"state": "offline", "pending": True}
    controller = Controller(settings, Database(settings.state_dir), editor, sync, lambda *args: {})
    with pytest.raises(UnsafeOperation):
        controller.change_mode("editing")
    assert controller.status()["mode"] == "error"
    assert editor.state == "running"
    with pytest.raises(UnsafeOperation):
        controller.sync()
    assert calls == ["sync"]


def test_uncertain_stop_never_starts_sync(settings):
    class UncertainStop(Editor):
        def stop(self):
            self.calls.append("stop")
            self.state = "unknown"
    calls = []
    editor = UncertainStop()
    controller = Controller(settings, Database(settings.state_dir), editor, lambda _: calls.append("sync") or {"state": "synced"}, lambda *args: calls.append("backup"))
    controller.change_mode("editing")
    calls.clear()
    with pytest.raises(UnsafeOperation):
        controller.change_mode("agent")
    assert calls == []
    assert controller.status()["mode"] == "error"


def test_pending_sync_allows_editor_without_reporting_synced(settings):
    editor = Editor()
    controller = Controller(settings, Database(settings.state_dir), editor, lambda _: {"state": "conflict", "pending": True, "message": "Manual resolution required"}, lambda *args: {})
    status = controller.change_mode("editing")
    assert status["mode"] == "editing"
    assert status["sync"]["state"] == "conflict"
    assert status["sync"]["last_synced_at"] is None


def test_interrupted_transition_restart_stops_editor_and_remains_error(settings):
    database = Database(settings.state_dir)
    database.set("mode", "transition")
    editor = Editor("running")
    controller = Controller(settings, database, editor, lambda _: pytest.fail("No sync during startup recovery"), lambda *args: pytest.fail("No backup during startup recovery"))
    assert editor.state == "stopped"
    assert controller.status()["mode"] == "error"


def test_editor_helper_uses_only_fixed_commands(monkeypatch):
    import json
    import subprocess
    from mwsecondbrain.controller import EditorHelper
    commands = []
    def run(command, **options):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps({"state": "stopped"}))
    monkeypatch.setattr(subprocess, "run", run)
    helper = EditorHelper()
    assert helper.status() == "stopped"
    helper.start()
    helper.stop()
    assert commands == [["sudo", "-n", "/usr/local/libexec/mwsb-editor", verb] for verb in ("status", "start", "stop")]
    with pytest.raises(UnsafeOperation):
        helper._run("status; touch /tmp/unsafe")
    assert len(commands) == 3


def test_backup_schedule_is_durable_daily_and_defers_while_editing(settings):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from mwsecondbrain.app import scheduler_tick
    calls = []
    now = datetime(2026, 10, 7, 2, 59, tzinfo=ZoneInfo("America/Sao_Paulo")).timestamp()
    controller = Controller(settings, Database(settings.state_dir), Editor(), lambda _: {"state": "offline", "pending": True}, lambda *args: calls.append("backup") or {"state": "ok"}, clock=lambda: now)
    scheduler_tick(controller, now)
    assert calls == []
    controller.change_mode("editing")
    now += 60
    scheduler_tick(controller, now)
    assert calls == []
    controller.change_mode("agent")
    assert calls == ["backup"]  # Editing recovery has its own required snapshot.
    calls.clear()
    scheduler_tick(controller, now)
    assert calls == ["backup"]
    scheduler_tick(controller, now + 60)
    assert calls == ["backup"]
    restarted = Controller(settings, Database(settings.state_dir), Editor(), lambda _: {"state": "offline", "pending": True}, lambda *args: calls.append("backup") or {"state": "ok"})
    scheduler_tick(restarted, now + 120)
    assert calls == ["backup"]
    scheduler_tick(restarted, now + 24 * 3600)
    assert calls == ["backup", "backup"]


def test_scheduled_backup_claim_is_under_controller_lock(settings):
    controller = Controller(settings, Database(settings.state_dir), Editor(), lambda _: {"state": "synced"}, lambda *args: {"state": "ok"})
    with controller.exclusive():
        with pytest.raises(UnsafeOperation):
            controller.backup(scheduled_day="2026-10-07")
    assert controller.database.get("backup_attempt_day") is None
    controller.backup(scheduled_day="2026-10-07")
    assert controller.backup(scheduled_day="2026-10-07") is None


def test_settings_reject_root_overlaps_and_non_https(tmp_path):
    with pytest.raises(ValueError):
        Settings(vault=Path("/"), state_dir=tmp_path / "state", backup_dir=tmp_path / "backups", public_origin="https://brain.example.test")
    with pytest.raises(ValueError):
        Settings(vault=tmp_path, state_dir=tmp_path / "state", backup_dir=tmp_path / "backups", public_origin="https://brain.example.test")
    with pytest.raises(ValueError):
        Settings(vault=tmp_path / "vault", state_dir=tmp_path / "state", backup_dir=tmp_path / "backups", public_origin="http://brain.example.test")


def test_frontend_serves_built_assets_only(settings, tmp_path):
    from dataclasses import replace
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html lang='pt-BR'>Built frontend</html>")
    (dist / "app.js").write_text("console.log('ready');")
    app = create_app(replace(settings, frontend_dir=dist), editor=Editor(), scheduler_enabled=False)
    with TestClient(app, base_url=settings.public_origin) as browser:
        assert "Built frontend" in browser.get("/").text
        assert browser.get("/app.js").status_code == 200
        assert browser.get("/../state/state.sqlite3").status_code == 404
        assert browser.get("/api/session").status_code == 401
        assert "Built frontend" in browser.get("/chat").text
        assert browser.get("/api/chat").status_code == 401


def test_environment_factory(monkeypatch, tmp_path):
    from mwsecondbrain.app import create_from_env
    for name, value in {"MWSB_VAULT_DIR": tmp_path / "vault", "MWSB_STATE_DIR": tmp_path / "state", "MWSB_BACKUP_DIR": tmp_path / "backups", "MWSB_PUBLIC_ORIGIN": "https://brain.example.test"}.items():
        monkeypatch.setenv(name, str(value))
    app = create_from_env()
    assert app.state.settings.vault == tmp_path / "vault"


def test_validation_errors_preserve_string_detail(client):
    browser, _, _ = client
    response = browser.post("/api/login", json={"password": []}, headers={"Origin": "https://brain.example.test"})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)


def test_confirmed_uppercase_sync_only_updates_timestamp(settings):
    result = {"state": "SYNCED", "message": "Remote verified", "pending": False, "head": "a" * 40, "remote_checked_now": True}
    controller = Controller(settings, Database(settings.state_dir), Editor(), lambda _: result, lambda *args: {"state": "success"}, clock=lambda: 1_800_000_000)
    controller.sync()
    original = controller.status()["sync"]["last_synced_at"]
    assert original is not None
    assert controller.status()["sync"]["state"] == "SYNCED"
    result["remote_checked_now"] = False
    controller.clock = lambda: 1_800_000_060
    controller.sync()
    assert controller.status()["sync"]["last_synced_at"] == original


def test_local_save_without_remote_confirmation_never_sets_sync_timestamp(settings):
    controller = Controller(settings, Database(settings.state_dir), Editor(), lambda _: {"state": "synced", "pending": False}, lambda *args: {"state": "success"})
    controller.sync()
    assert controller.status()["sync"]["last_synced_at"] is None


def test_editing_end_backups_saved_vault_before_reconciliation(settings):
    editor = Editor()
    calls = []
    def sync(_):
        calls.append("sync")
        return {"state": "SYNCED", "pending": False}
    def backup(*args):
        assert editor.state == "stopped"
        calls.append("backup")
        return {"state": "success"}
    controller = Controller(settings, Database(settings.state_dir), editor, sync, backup)
    controller.change_mode("editing")
    calls.clear()
    controller.change_mode("agent")
    assert calls == ["backup", "sync"]


def test_backup_failure_during_editing_end_blocks_sync(settings):
    calls = []
    controller = Controller(settings, Database(settings.state_dir), Editor(), lambda _: calls.append("sync") or {"state": "pending", "pending": True}, lambda *args: {"state": "error", "message": "Disk full"})
    controller.change_mode("editing")
    calls.clear()
    with pytest.raises(UnsafeOperation, match="Backup"):
        controller.change_mode("agent")
    assert calls == []
    assert controller.status()["mode"] == "error"
    assert controller.status()["backup"]["last_success_at"] is None


def test_backup_requirement_survives_failed_transition_and_restart(settings):
    calls = []
    database = Database(settings.state_dir)
    controller = Controller(settings, database, Editor(), lambda _: {"state": "offline", "pending": True}, lambda *args: {"state": "error"})
    controller.change_mode("editing")
    with pytest.raises(UnsafeOperation):
        controller.change_mode("agent")
    assert database.get("backup_required") is True
    recovered = Controller(settings, Database(settings.state_dir), Editor(), lambda _: calls.append("sync") or {"state": "offline", "pending": True}, lambda *args: calls.append("backup") or {"state": "success"})
    recovered.change_mode("agent")
    assert calls == ["backup", "sync"]
    assert database.get("backup_required") is False


def test_flock_prevents_other_process_writes(settings):
    import subprocess
    import sys
    controller = Controller(settings, Database(settings.state_dir), Editor(), lambda _: {}, lambda *args: {})
    probe = """import fcntl, sys
with open(sys.argv[1], 'a') as lock:
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(23)
"""
    with controller.exclusive():
        result = subprocess.run([sys.executable, "-c", probe, str(controller.lock_path)])
    assert result.returncode == 23
    result = subprocess.run([sys.executable, "-c", probe, str(controller.lock_path)])
    assert result.returncode == 0


def test_cli_password_initialization_and_reset_use_getpass(monkeypatch, tmp_path, capsys):
    from mwsecondbrain.cli import main
    for name, value in {"MWSB_VAULT_DIR": tmp_path / "vault", "MWSB_STATE_DIR": tmp_path / "state", "MWSB_BACKUP_DIR": tmp_path / "backups", "MWSB_PUBLIC_ORIGIN": "https://brain.example.test"}.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.setattr("getpass.getpass", lambda prompt: "first strong password")
    assert main(["init-password"]) == 0
    database = Database(tmp_path / "state")
    auth = Auth(database)
    token, _ = auth.login("first strong password", "127.0.0.1")
    assert main(["init-password"]) == 1
    assert "already initialized" in capsys.readouterr().err
    monkeypatch.setattr("getpass.getpass", lambda prompt: "second strong password")
    assert main(["reset-password"]) == 0
    assert auth.session(token) is None
    auth.login("second strong password", "127.0.0.1")


def test_cli_confirmation_mismatch_never_changes_password(monkeypatch, tmp_path, capsys):
    from mwsecondbrain.cli import main
    for name, value in {"MWSB_VAULT_DIR": tmp_path / "vault", "MWSB_STATE_DIR": tmp_path / "state", "MWSB_BACKUP_DIR": tmp_path / "backups", "MWSB_PUBLIC_ORIGIN": "https://brain.example.test"}.items():
        monkeypatch.setenv(name, str(value))
    passwords = iter(["first strong password", "different strong password"])
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(passwords))
    assert main(["init-password"]) == 1
    assert "do not match" in capsys.readouterr().err
    with Database(tmp_path / "state").connect() as connection:
        assert connection.execute("SELECT value FROM metadata WHERE key='password_hash'").fetchone() is None


def test_logout_stops_editor_without_running_sync_or_backup(client):
    browser, app, _ = client
    csrf = login(browser)
    browser.post("/api/mode", json={"mode": "editing"}, headers=mutation(browser, csrf))
    app.state.controller.sync_callback = lambda _: pytest.fail("Logout must not reconcile the vault")
    app.state.controller.backup_callback = lambda *args: pytest.fail("Logout must not write backups")
    token = browser.cookies.get("mwsb_session")
    response = browser.post("/api/logout", headers=mutation(browser, csrf))
    assert response.status_code == 200
    assert response.json() == {"authenticated": False}
    assert app.state.auth.session(token) is None
    assert app.state.controller.editor.state == "stopped"
    assert app.state.controller.status()["mode"] == "error"
    assert app.state.database.get("backup_required") is True


def test_logout_revokes_session_even_when_editor_stop_fails(client):
    browser, app, _ = client
    csrf = login(browser)
    browser.post("/api/mode", json={"mode": "editing"}, headers=mutation(browser, csrf))
    token = browser.cookies.get("mwsb_session")
    class FailingStop(Editor):
        def stop(self):
            raise RuntimeError("Graceful editor shutdown timed out")
    app.state.controller.editor = FailingStop("running")
    response = browser.post("/api/logout", headers=mutation(browser, csrf))
    assert response.status_code == 200
    assert response.json()["authenticated"] is False
    assert isinstance(response.json()["warning"], str)
    assert app.state.auth.session(token) is None
    assert browser.get("/api/session").status_code == 401
    assert app.state.controller.status()["mode"] == "error"
    assert app.state.controller.editor.state == "running"
    with pytest.raises(UnsafeOperation):
        app.state.controller.sync()


def test_logout_revokes_session_when_controller_lock_is_busy(client):
    browser, app, _ = client
    csrf = login(browser)
    browser.post("/api/mode", json={"mode": "editing"}, headers=mutation(browser, csrf))
    token = browser.cookies.get("mwsb_session")
    with app.state.controller.exclusive():
        response = browser.post("/api/logout", headers=mutation(browser, csrf))
    assert response.status_code == 200
    assert response.json()["authenticated"] is False
    assert response.json()["warning"]
    assert app.state.auth.session(token) is None


def test_editor_helper_allows_bounded_graceful_shutdown_timeout(monkeypatch):
    import subprocess
    from mwsecondbrain.controller import EditorHelper
    timeouts = []
    def run(command, **options):
        timeouts.append(options["timeout"])
        return subprocess.CompletedProcess(command, 0, '{"state":"stopped"}')
    monkeypatch.setattr(subprocess, "run", run)
    EditorHelper().stop()
    assert timeouts == [120]
