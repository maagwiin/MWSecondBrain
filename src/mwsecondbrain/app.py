"""Secure phase-one HTTP surface and conservative background scheduling."""

import asyncio
import hmac
import json
import os
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime, time as daytime
from zoneinfo import ZoneInfo
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .auth import ABSOLUTE_SECONDS, COOKIE_NAME, Auth, InvalidCredentials, Throttled
from .config import Settings
from .controller import Controller, UnsafeOperation
from .db import Database
from .chat import ChatStore, ChatWorker, RuntimeFailure

LOGIN_BODY_LIMIT = 4096


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    password: str = Field(min_length=1, max_length=1024)


class ModeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: str = Field(pattern="^(editing|agent)$")


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(default="", max_length=32768)
    attachment_ids: list[str] = Field(default_factory=list, max_length=8)
    model: str | None = Field(default=None, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=256)


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: str = Field(min_length=1, max_length=64)


class CaptureBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    paused: bool


def scheduler_tick(controller, now):
    """Perform one minute of work; daily retries wait for an explicit next day."""
    applied = 0
    try:
        if controller.notes.pending_count():
            applied = controller.apply_note_operations()["applied"]
        if not applied:
            controller.sync()
    except UnsafeOperation:
        pass
    local = datetime.fromtimestamp(now, ZoneInfo("America/Sao_Paulo"))
    day = local.date().isoformat()
    if local.time() >= daytime(3) and controller.database.get("backup_attempt_day") != day:
        # Defer during editing; backup will run once editing has ended.
        if controller.status()["mode"] == "agent":
            try:
                controller.backup(scheduled_day=day)
            except UnsafeOperation:
                pass


def create_app(settings=None, *, sync_callback=None, backup_callback=None,
               editor=None, clock=time.time, scheduler_enabled=True, runtime=None,
               runtime_verified=None, chat_worker_enabled=True, note_policy=False,
               attachments_resolver=None, completion_hook=None):
    settings = settings if settings is not None else Settings.from_env()
    database = Database(settings.state_dir)
    auth = Auth(database, clock)
    controller = Controller(settings, database, editor, sync_callback, backup_callback, clock)
    if note_policy is not False:
        from .notes import NotesService
        controller.notes = NotesService(controller, note_policy)
    store = ChatStore(database, clock)
    verified = runtime_verified if runtime_verified is not None else os.environ.get("MWSB_SUBSCRIPTION_VERIFIED") == "1"
    if runtime is None and os.environ.get("MWSB_CHAT_ENABLED") == "1" and verified:
        auth_dir = os.environ.get("MWSB_AUTH_DIR")
        if auth_dir:
            try:
                from .codex import CodexRuntime
                runtime = CodexRuntime(Path(auth_dir), settings.state_dir / "runtime")
            except (ImportError, OSError, ValueError, RuntimeFailure):
                runtime = None
    attachments = None
    attachment_router = None
    try:
        from .attachments import Attachments, router
        attachments = Attachments(database, settings.state_dir / "attachments")
        attachment_router = router
    except ImportError:
        pass
    worker = ChatWorker(store, controller, runtime, completion_hook,
                        attachments_resolver or (attachments.resolve if attachments else None), verified)
    telegram = None
    if attachments is not None:
        try:
            from .telegram import TelegramService
            telegram = TelegramService(store, attachments, os.environ.get("MWSB_TELEGRAM_CONFIG", "/etc/mwsecondbrain/telegram.json"))
        except ImportError:
            pass
    if not chat_worker_enabled:
        worker.refresh_runtime()

    async def schedule():
        while True:
            await asyncio.sleep(60)
            await asyncio.to_thread(scheduler_tick, controller, clock())

    @asynccontextmanager
    async def lifespan(application):
        task = asyncio.create_task(schedule()) if scheduler_enabled else None
        if chat_worker_enabled:
            await worker.start()
        if telegram is not None:
            await telegram.start()
        try:
            yield
        finally:
            if telegram is not None:
                await telegram.stop()
            if chat_worker_enabled:
                await worker.stop()
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.database = database
    app.state.auth = auth
    app.state.controller = controller
    app.state.chat_store = store
    app.state.chat_worker = worker
    app.state.attachments = attachments
    app.state.telegram = telegram

    @app.middleware("http")
    async def security_headers(request, call_next):
        if request.url.path in {"/api/login", "/api/chat/messages"}:
            body_limit = LOGIN_BODY_LIMIT if request.url.path == "/api/login" else 262144
            length = request.headers.get("content-length")
            try:
                if length is not None and (int(length) < 0 or int(length) > body_limit):
                    return JSONResponse({"detail": "Request too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
            # Bound chunked bodies too, before JSON parsing allocates their full content.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > body_limit:
                    return JSONResponse({"detail": "Request too large"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def require_origin(request: Request):
        if request.headers.get("origin") != settings.public_origin:
            raise HTTPException(403, "Origin rejected")

    def require_session(request: Request):
        session = auth.session(request.cookies.get(COOKIE_NAME))
        if session is None:
            raise HTTPException(401, "Authentication required")
        return session

    def require_mutation(request: Request, session=Depends(require_session)):
        require_origin(request)
        supplied = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(supplied.encode(), session["csrf"].encode()):
            raise HTTPException(403, "CSRF token rejected")
        return session

    if attachments is not None:
        app.include_router(attachment_router(attachments, require_session, require_mutation))

    @app.exception_handler(UnsafeOperation)
    async def unsafe_operation(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # Do not echo rejected passwords or request bodies in error responses.
        return JSONResponse({"detail": "Invalid request"}, status_code=422)

    @app.exception_handler(ValueError)
    async def invalid_chat_request(request, error):
        return JSONResponse({"detail": "Invalid chat request"}, status_code=400)

    @app.exception_handler(RuntimeFailure)
    async def runtime_unavailable(request, error):
        return JSONResponse({"detail": "Subscription runtime unavailable"}, status_code=503)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/api/session")
    def session(current=Depends(require_session)):
        return {"authenticated": True, "csrf_token": current["csrf"]}

    @app.post("/api/login")
    def login(body: LoginBody, request: Request, response: Response):
        require_origin(request)
        # Never trust browser-controlled X-Forwarded-For. Proxy trust belongs in Uvicorn.
        ip = request.client.host if request.client else "unknown"
        try:
            token, csrf = auth.login(body.password, ip)
        except Throttled:
            raise HTTPException(429, "Too many login failures", headers={"Retry-After": "900"}) from None
        except InvalidCredentials:
            raise HTTPException(401, "Invalid credentials") from None
        response.set_cookie(COOKIE_NAME, token, max_age=ABSOLUTE_SECONDS, httponly=True,
                            secure=True, samesite="strict", path="/")
        return {"csrf_token": csrf}

    @app.post("/api/logout")
    def logout(request: Request, response: Response, current=Depends(require_mutation)):
        # Revocation comes first, even if the editor or helper cannot be stopped.
        auth.logout(request.cookies[COOKIE_NAME])
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=True, samesite="strict")
        result = {"authenticated": False}
        try:
            controller.close_editor_for_logout()
        except UnsafeOperation:
            result["warning"] = "Session revoked; editor shutdown could not be confirmed. Recovery is required."
        return result

    @app.get("/api/status")
    def status(current=Depends(require_session)):
        result = controller.status()
        availability = worker.availability()
        result["phase2"] = {"ready": availability["ready"], "reason": availability["reason"]}
        return result

    @app.get("/api/chat")
    def chat(conversation_id: str | None = None, current=Depends(require_session)):
        return store.view(conversation_id)

    @app.get("/api/chat/conversations")
    def conversations(current=Depends(require_session)):
        return {"conversations": store.conversations()}

    @app.post("/api/chat/messages", status_code=202)
    def chat_message(body: ChatBody, current=Depends(require_mutation)):
        if body.attachment_ids:
            if attachments is None:
                raise HTTPException(503, "Attachments unavailable")
            for identifier in body.attachment_ids:
                attachments.metadata(identifier)
        if body.model and worker.catalog and body.model not in {model["id"] for model in worker.catalog}:
            raise HTTPException(400, "Model is not in the account catalog")
        return store.enqueue(body.text, "web", body.idempotency_key, body.attachment_ids, body.model)

    @app.post("/api/chat/cancel")
    def cancel_chat(body: CancelBody, current=Depends(require_mutation)):
        return {"cancel_requested": store.cancel(body.job_id)}

    @app.post("/api/chat/new")
    def new_chat(current=Depends(require_mutation)):
        return {"conversation_id": store.new_conversation()}

    @app.post("/api/chat/capture")
    def capture_chat(body: CaptureBody, current=Depends(require_mutation)):
        # A successful pause cannot race a note replacement in another request.
        with controller.exclusive():
            store.set_capture(body.paused)
        return {"capture_paused": body.paused}

    @app.post("/api/chat/resume")
    def resume_chat(current=Depends(require_mutation)):
        return worker.resume()

    @app.get("/api/models")
    def models(current=Depends(require_session)):
        if not worker.catalog:
            worker.refresh_runtime()
        return worker.availability()

    @app.get("/api/chat/events")
    async def chat_events(request: Request, current=Depends(require_session)):
        try:
            event_id = int(request.headers.get("last-event-id", "0"))
            if not 0 <= event_id < 2**63:
                raise ValueError()
        except ValueError:
            raise HTTPException(400, "Invalid Last-Event-ID") from None
        token = request.cookies.get(COOKIE_NAME)
        async def stream():
            last, idle = event_id, 0
            while not await request.is_disconnected():
                if await asyncio.to_thread(auth.session, token) is None:
                    return
                events = await asyncio.to_thread(store.events_after, last)
                for event in events:
                    if await asyncio.to_thread(auth.session, token) is None:
                        return
                    yield f"id: {event['id']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    last = event["id"]
                idle = 0 if events else idle + 1
                if idle >= 30:
                    yield ": heartbeat\n\n"
                    idle = 0
                await asyncio.sleep(0.5)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/api/mode")
    def mode(body: ModeBody, current=Depends(require_mutation)):
        return controller.change_mode(body.mode)

    @app.post("/api/sync")
    def sync(current=Depends(require_mutation)):
        return controller.sync()

    @app.post("/api/backup")
    def backup(current=Depends(require_mutation)):
        return controller.backup()

    @app.get("/api/proxy-auth")
    def proxy_auth(current=Depends(require_session)):
        if controller.status()["mode"] != "editing":
            raise HTTPException(403, "Editor access requires confirmed editing mode")
        return Response(status_code=200)

    if settings.frontend_dir and (settings.frontend_dir / "index.html").is_file():
        @app.get("/chat")
        def chat_shell():
            return FileResponse(settings.frontend_dir / "index.html")

        app.mount("/", StaticFiles(directory=settings.frontend_dir, html=True), name="frontend")
    else:
        @app.get("/")
        def frontend_unavailable():
            return JSONResponse({"detail": "Frontend has not been built"}, status_code=503)
    return app


def create_from_env():
    """Uvicorn production factory: mwsecondbrain.app:create_from_env --factory."""
    return create_app(Settings.from_env())
