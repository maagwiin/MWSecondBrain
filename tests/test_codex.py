import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from pathlib import Path

import pytest

from mwsecondbrain.codex import AuthRequired, Cancelled, CodexRuntime, QuotaExceeded, RuntimeFailure, TOOL_SCHEMAS, _Session


@pytest.fixture
def runtime(tmp_path):
    auth = tmp_path / "auth"
    work = tmp_path / "snapshot"
    auth.mkdir(mode=0o700)
    work.mkdir()
    (work / ".attachments").mkdir()
    record = {"issuer": "https://auth.openai.com", "subject": "fixture-account", "client_id": "oaiapp_fixture",
              "access_token": "fixture-oauth-secret", "refresh_token": "fixture-refresh-secret",
              "id_token": "fixture-id", "token_type": "Bearer", "expires_in": 3600,
              "scopes": ["resource.invoke", "chatgpt.tokens.use.direct"],
              "saved_at": "2099-01-01T00:00:00.000Z"}
    credential = auth / "credentials.json"
    credential.write_text(json.dumps(record))
    credential.chmod(0o600)
    return CodexRuntime(auth, work)


CATALOG = {"models": [
    {"slug": "hidden", "display_name": "Hidden", "visibility": "hide"},
    {"slug": "text-model", "display_name": "Text model", "visibility": "list", "input_modalities": ["text"]},
    {"slug": "account-model", "display_name": "Account model", "visibility": "list", "input_modalities": ["text", "image"], "is_default": True},
]}


def catalog(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "_request_catalog", lambda _: CATALOG)


def fake_server(tmp_path, mode="complete"):
    """A protocol peer: exercises actual pipes and process cleanup, never OpenAI."""
    script = tmp_path / "peer.py"
    script.write_text('''import json,sys,time
mode=sys.argv[1]
def send(x): print(json.dumps(x),flush=True)
for line in sys.stdin:
 x=json.loads(line); m=x.get("method"); i=x.get("id")
 if m=="initialize": send({"id":i,"result":{}})
 elif m=="thread/start":
  if "dynamicTools" not in x["params"] or x["params"].get("sandbox")!="read-only":
   send({"id":i,"error":{"code":-32600,"message":"Invalid protected runtime setup"}})
  else: send({"id":i,"result":{"thread":{"id":"thread-fixture"}}})
 elif m=="thread/inject_items": send({"id":i,"result":{}})
 elif m=="turn/start":
  send({"id":i,"result":{"turn":{"id":"turn-fixture","status":"inProgress"}}})
  if mode=="tool":
   send({"id":"request-tool","method":"item/tool/call","params":{"threadId":"thread-fixture","turnId":"turn-fixture","callId":"call-1","namespace":"brain","tool":"brain_read","arguments":{"path":"note.md"}}})
  elif mode=="unauthorized":
   send({"id":"request-tool","method":"item/tool/call","params":{"threadId":"thread-fixture","turnId":"turn-fixture","callId":"call-1","namespace":"brain","tool":"shell","arguments":{"cmd":"cat secret"}}})
  elif mode=="quota": send({"method":"turn/completed","params":{"threadId":"thread-fixture","turn":{"id":"turn-fixture","status":"failed","error":{"codexErrorInfo":"usageLimitExceeded","message":"SECRET must never print"}}}})
  elif mode=="hang": pass
  elif mode=="rpc": send({"id":i,"error":{"code":-32600,"message":"SECRET must never print"}})
  elif mode=="wrongturn":
   send({"method":"item/agentMessage/delta","params":{"threadId":"thread-fixture","turnId":"another-turn","delta":"unexpected"}})
   send({"method":"turn/completed","params":{"threadId":"thread-fixture","turn":{"id":"turn-fixture","status":"completed"}}})
  else:
   send({"method":"item/agentMessage/delta","params":{"threadId":"thread-fixture","turnId":"turn-fixture","itemId":"answer","delta":"Hello "}})
   send({"method":"item/agentMessage/delta","params":{"threadId":"thread-fixture","turnId":"turn-fixture","itemId":"answer","delta":"world"}})
   send({"method":"turn/completed","params":{"threadId":"thread-fixture","turn":{"id":"turn-fixture","status":"completed"}}})
 elif m=="turn/interrupt":
  send({"id":i,"result":{}})
  send({"method":"turn/completed","params":{"threadId":"thread-fixture","turn":{"id":"turn-fixture","status":"interrupted"}}})
 elif i=="request-tool":
  if mode=="tool" and not x.get("result",{}).get("success"): sys.exit(4)
  send({"method":"item/agentMessage/delta","params":{"threadId":"thread-fixture","turnId":"turn-fixture","itemId":"answer","delta":"Tool result accepted"}})
  send({"method":"turn/completed","params":{"threadId":"thread-fixture","turn":{"id":"turn-fixture","status":"completed"}}})
''')
    return script


def peer(runtime, monkeypatch, tmp_path, mode="complete"):
    script = fake_server(tmp_path, mode)
    processes = []
    def start(token):
        process = subprocess.Popen([sys.executable, str(script), mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        processes.append(process)
        return process
    monkeypatch.setattr(runtime, "_start_process", start)
    return processes


def test_models_are_account_catalog_only_and_images_explicit(runtime, monkeypatch):
    catalog(runtime, monkeypatch)
    assert runtime.models() == [
        {"id": "text-model", "label": "Text model", "supports_images": False},
        {"id": "account-model", "label": "Account model", "supports_images": True},
    ]


def test_missing_scope_unsafe_mode_and_symlinks_require_reconnect(runtime):
    file = runtime.auth_dir / "credentials.json"
    original = file.read_text()
    file.chmod(0o644)
    with pytest.raises(AuthRequired): runtime.models()
    file.chmod(0o600)
    record = json.loads(original)
    record["scopes"] = ["openid"]
    file.write_text(json.dumps(record))
    with pytest.raises(AuthRequired): runtime.models()
    file.unlink()
    target = runtime.auth_dir / "other.json"
    target.write_text(original)
    target.chmod(0o600)
    file.symlink_to(target)
    with pytest.raises(AuthRequired): runtime.models()


def test_completed_response_streams_and_reaps_process(runtime, monkeypatch, tmp_path):
    catalog(runtime, monkeypatch)
    processes = peer(runtime, monkeypatch, tmp_path)
    deltas = []
    result = runtime.run([{"role": "user", "content": "hi"}], None, lambda *_: {}, deltas.append, lambda: False)
    assert result == "Hello world"
    assert deltas == ["Hello ", "world"]
    assert processes[0].poll() is not None


def test_dynamic_tool_dispatch_is_allowlisted(runtime, monkeypatch, tmp_path):
    catalog(runtime, monkeypatch)
    peer(runtime, monkeypatch, tmp_path, "tool")
    calls = []
    def tool(name, arguments):
        calls.append((name, arguments))
        return {"content": "safe snapshot text"}
    assert runtime.run([{"role": "user", "content": "read note"}], None, tool, lambda _: None, lambda: False) == "Tool result accepted"
    assert calls == [("brain_read", {"path": "note.md"})]


def test_unapproved_tool_request_never_reaches_callback(runtime, monkeypatch, tmp_path):
    catalog(runtime, monkeypatch)
    peer(runtime, monkeypatch, tmp_path, "unauthorized")
    calls = []
    with pytest.raises(RuntimeFailure):
        runtime.run([{"role": "user", "content": "read"}], None, lambda *args: calls.append(args), lambda _: None, lambda: False)
    assert calls == []


def test_quota_has_no_retry_and_never_exposes_remote_error(runtime, monkeypatch, tmp_path):
    catalog(runtime, monkeypatch)
    processes = peer(runtime, monkeypatch, tmp_path, "quota")
    with pytest.raises(QuotaExceeded) as error:
        runtime.run([{"role": "user", "content": "hi"}], None, lambda *_: {}, lambda _: None, lambda: False)
    assert "SECRET" not in str(error.value)
    assert len(processes) == 1
    assert processes[0].poll() is not None


def test_delta_from_another_turn_never_reaches_user(runtime, monkeypatch, tmp_path):
    catalog(runtime, monkeypatch)
    peer(runtime, monkeypatch, tmp_path, "wrongturn")
    deltas = []
    with pytest.raises(RuntimeFailure):
        runtime.run([{"role": "user", "content": "hi"}], None, lambda *_: {}, deltas.append, lambda: False)
    assert deltas == []


def test_cancel_interrupts_and_reaps_hung_peer(runtime, monkeypatch, tmp_path):
    catalog(runtime, monkeypatch)
    processes = peer(runtime, monkeypatch, tmp_path, "hang")
    start = time.monotonic()
    with pytest.raises(Cancelled):
        runtime.run([{"role": "user", "content": "hi"}], None, lambda *_: {}, lambda _: None,
                    lambda: time.monotonic() - start > 0.2)
    assert time.monotonic() - start < 3
    assert processes[0].poll() is not None


def test_unknown_model_and_external_or_symlink_images_reject_before_start(runtime, monkeypatch, tmp_path):
    catalog(runtime, monkeypatch)
    calls = []
    monkeypatch.setattr(runtime, "_start_process", lambda *_: calls.append(True))
    for model, image in [("not-in-catalog", None), ("text-model", runtime.work_dir / ".attachments" / "pic.png"),
                         ("account-model", tmp_path / "outside.png")]:
        if image: image.write_bytes(b"fixture")
        with pytest.raises(RuntimeFailure):
            runtime.run([{"role": "user", "content": "image"}], model, lambda *_: {}, lambda _: None,
                        lambda: False, [image] if image else [])
    link = runtime.work_dir / ".attachments" / "link.png"
    link.symlink_to(tmp_path / "outside.png")
    with pytest.raises(RuntimeFailure):
        runtime.run([{"role": "user", "content": "image"}], "account-model", lambda *_: {}, lambda _: None,
                    lambda: False, [link])
    assert calls == []


def test_bwrap_command_mounts_only_snapshot_runtime_and_system_baseline(runtime, monkeypatch):
    command = runtime._confined_command(["/usr/bin/codex", "app-server"])
    assert "--ro-bind" in command
    assert command[command.index(str(runtime.work_dir)) + 1] == "/workspace"
    assert str(runtime.auth_dir / "credentials.json") not in command
    assert str(runtime.auth_dir) not in command
    assert "/workspace" in command
    assert command[-2:] == ["/usr/bin/codex", "app-server"]


def test_environment_drops_keys_and_global_home(runtime, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "paid-key-forbidden")
    monkeypatch.setenv("CODEX_HOME", "/global/codex")
    env = runtime._child_environment("fresh-oauth")
    assert env["CODEX_HOME"] == "/runtime"
    assert env["ACCESS_TOKEN"] == "fresh-oauth"
    assert "OPENAI_API_KEY" not in env
    assert "HOME" not in env


def expire(runtime):
    file = runtime.auth_dir / "credentials.json"
    record = json.loads(file.read_text())
    record["saved_at"] = "2000-01-01T00:00:00.000Z"
    file.write_text(json.dumps(record))


def test_refresh_is_serialized_and_second_caller_reads_rotated_token(runtime, monkeypatch):
    expire(runtime)
    calls = []
    def refresh():
        calls.append(True)
        time.sleep(0.05)
        file = runtime.auth_dir / "credentials.json"
        record = json.loads(file.read_text())
        record.update(access_token="rotated", saved_at=datetime.now(timezone.utc).isoformat())
        file.write_text(json.dumps(record))
    monkeypatch.setattr(runtime, "_refresh", refresh)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: runtime._credentials(), range(2)))
    assert len(calls) == 1
    assert [item["access_token"] for item in results] == ["rotated", "rotated"]


@pytest.mark.parametrize("response, expected", [
    ({"ok": False, "error": "auth_required"}, AuthRequired),
    ({"ok": False, "error": "quota_exceeded"}, QuotaExceeded),
    ([], RuntimeFailure),
])
def test_expired_credentials_map_refresh_errors_without_inference(runtime, monkeypatch, response, expected):
    expire(runtime)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, json.dumps(response)))
    with pytest.raises(expected) as error:
        runtime._credentials()
    assert "fixture-oauth-secret" not in str(error.value)


def test_malformed_scope_and_catalog_label_are_typed(runtime, monkeypatch):
    file = runtime.auth_dir / "credentials.json"
    record = json.loads(file.read_text())
    record["scopes"].append({"SECRET": "must never print"})
    file.write_text(json.dumps(record))
    with pytest.raises(AuthRequired) as error:
        runtime._credentials()
    assert "SECRET" not in str(error.value)
    record["scopes"].pop()
    file.write_text(json.dumps(record))
    monkeypatch.setattr(runtime, "_request_catalog", lambda _: {"models": [{"slug": "safe", "display_name": {"invalid": True}, "visibility": "list"}]})
    assert runtime.models()[0]["label"] == "safe"


@pytest.mark.parametrize("auth", ["/usr/share/oauth", "/tmp/../usr/share/oauth", "/etc/ssl/oauth"])
def test_auth_cannot_be_inside_system_baseline(auth, tmp_path):
    with pytest.raises(RuntimeFailure):
        CodexRuntime(auth, tmp_path / "snapshot")


def test_fresh_runtime_creates_snapshot_and_finds_release_helper(tmp_path, monkeypatch):
    import mwsecondbrain.codex as codex
    release = tmp_path / "release"
    helper = release / "tools" / "account-login" / "refresh.mjs"
    helper.parent.mkdir(parents=True)
    helper.write_text("fixture")
    helper.chmod(0o644)
    monkeypatch.setattr(codex, "__file__", str(release / ".venv/lib/python3.10/site-packages/mwsecondbrain/codex.py"))
    monkeypatch.setattr(sys, "prefix", str(release / ".venv"))
    fresh = CodexRuntime(tmp_path / "auth", tmp_path / "new/snapshot")
    assert fresh.work_dir.is_dir()
    assert fresh._helper == helper


@pytest.mark.skipif(os.environ.get("MWSB_TEST_CONFINEMENT") != "1", reason="Requires host user namespaces, not managed sandbox")
def test_real_bwrap_denies_outside_file_and_snapshot_write(runtime, tmp_path):
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("fixture-secret")
    note = runtime.work_dir / "note.md"
    note.write_text("unchanged")
    script = "import pathlib; p=pathlib.Path('/workspace/note.md'); assert p.read_text()=='unchanged'; " \
             "assert not pathlib.Path('" + str(outside) + "').exists(); " \
             "p.write_text('forbidden')"
    result = subprocess.run(runtime._confined_command(["/usr/bin/python3", "-c", script]),
                            env=runtime._child_environment("fixture-token"), capture_output=True, text=True)
    assert result.returncode != 0
    assert "Read-only file system" in result.stderr
    assert note.read_text() == "unchanged"


@pytest.mark.skipif(os.environ.get("MWSB_TEST_APP_SERVER") != "1", reason="Requires installed Codex and host user namespaces")
def test_real_app_server_initializes_confined_without_inference(runtime):
    session = _Session(runtime._start_process("dummy-no-account-token"), lambda *_: {}, lambda _: None, lambda: False)
    try:
        initialized = session.rpc("initialize", {"clientInfo": {"name": "MWSecondBrain", "version": "0.1.0"}, "capabilities": {"experimentalApi": True}})
        assert initialized
        session.send({"method": "initialized", "params": {}})
        thread = session.rpc("thread/start", {"cwd": "/workspace", "model": "fixture-model", "approvalPolicy": "never", "sandbox": "read-only", "dynamicTools": [{"type": "namespace", "name": "brain", "description": "Test controlled tools", "tools": [{"type": "function", "name": name, "description": name, "inputSchema": schema} for name, schema in TOOL_SCHEMAS.items()]}]})
        assert thread["thread"]["id"]
        assert thread["cwd"] == "/workspace"
        # No turn/start: this check never calls a model or uses real OAuth.
    finally:
        session.close()
    assert session.process.poll() is not None


@pytest.mark.skipif(os.environ.get("MWSB_TEST_APP_SERVER") != "1", reason="Requires installed Codex and host user namespaces")
def test_real_app_server_sends_only_controlled_tools_to_offline_provider(runtime, monkeypatch):
    captured = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            captured.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"fixture quota","code":"insufficient_quota"}}')
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_confine = runtime._confined_command
    def offline(command):
        command = [item.replace('base_url="https://api.openai.com/v1"', 'base_url="http://127.0.0.1:' + str(server.server_port) + '/v1"') for item in command]
        return real_confine(command)
    monkeypatch.setattr(runtime, "_confined_command", offline)
    catalog(runtime, monkeypatch)
    try:
        with pytest.raises((QuotaExceeded, RuntimeFailure)):
            runtime.run([{"role": "user", "content": "offline test"}], None, lambda *_: {}, lambda _: None, lambda: False)
        assert len(captured) == 1
        tools = captured[0]["tools"]
        assert [(item["type"], item["name"]) for item in tools] == [("namespace", "brain")]
        assert sorted(item["name"] for item in tools[0]["tools"]) == sorted(TOOL_SCHEMAS)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
