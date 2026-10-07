"""Secure phase-one HTTP surface and conservative background scheduling."""

import asyncio
import hmac
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime, time as daytime
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .auth import ABSOLUTE_SECONDS, COOKIE_NAME, Auth, InvalidCredentials, Throttled
from .config import Settings
from .controller import Controller, UnsafeOperation
from .db import Database

LOGIN_BODY_LIMIT = 4096


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    password: str = Field(min_length=1, max_length=1024)


class ModeBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: str = Field(pattern="^(editing|agent)$")


def scheduler_tick(controller, now):
    """Perform one minute of work; daily retries wait for an explicit next day."""
    try:
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
               editor=None, clock=time.time, scheduler_enabled=True):
    settings = settings if settings is not None else Settings.from_env()
    database = Database(settings.state_dir)
    auth = Auth(database, clock)
    controller = Controller(settings, database, editor, sync_callback, backup_callback, clock)

    async def schedule():
        while True:
            await asyncio.sleep(60)
            await asyncio.to_thread(scheduler_tick, controller, clock())

    @asynccontextmanager
    async def lifespan(application):
        task = asyncio.create_task(schedule()) if scheduler_enabled else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.database = database
    app.state.auth = auth
    app.state.controller = controller

    @app.middleware("http")
    async def security_headers(request, call_next):
        if request.url.path == "/api/login":
            length = request.headers.get("content-length")
            try:
                if length is not None and (int(length) < 0 or int(length) > LOGIN_BODY_LIMIT):
                    return JSONResponse({"detail": "Login request too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
            # Bound chunked bodies too, before JSON parsing allocates their full content.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > LOGIN_BODY_LIMIT:
                    return JSONResponse({"detail": "Login request too large"}, status_code=413)
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

    @app.exception_handler(UnsafeOperation)
    async def unsafe_operation(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # Do not echo rejected passwords or request bodies in error responses.
        return JSONResponse({"detail": "Invalid request"}, status_code=422)

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
        auth.logout(request.cookies[COOKIE_NAME])
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, secure=True, samesite="strict")
        return {"authenticated": False}

    @app.get("/api/status")
    def status(current=Depends(require_session)):
        return controller.status()

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
