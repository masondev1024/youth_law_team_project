"""Local owner-only consent, card and observation rehearsal browser app."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.preparation_boundary import _HEADERS
from app.preparation_models import PreparationError
from app.preparation_routes import (
    _read_request_body,
    get_preparation_options,
    post_preparation_card,
)
from app.preparation_service import catalog_readiness
from app.protected_study import _has_forwarding_headers, _is_loopback, _route_name, _Runtime
from app.rehearsal_sessions import (
    SESSION_SECONDS,
    BrowserSession,
    ConsentRequest,
    LoginInput,
    ObservationRequest,
    RehearsalError,
    RehearsalSessions,
    RestoreRequest,
    StrictInput,
    TimingRequest,
)
from app.study_runtime import PrivateStudyRuntime, StudyRuntimeUnavailable
from app.study_storage import StudyStorageError

_HERE = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=_HERE / "templates")
_ACTIONS = {
    "consent": ConsentRequest,
    "restore": RestoreRequest,
    "start": StrictInput,
    "restart": StrictInput,
    "timing": TimingRequest,
    "observations": ObservationRequest,
    "withdraw": StrictInput,
}


def _storage_status(code: str) -> int:
    if code in {"invalid_record", "invalid_request_key"}:
        return 422
    if code in {"conflict", "revoked"}:
        return 409
    if code == "expired":
        return 410
    if code == "admission_closed":
        return 403
    return 503


async def _payload(request: Request, model: type[StrictInput]) -> StrictInput:
    try:
        return model.model_validate(json.loads(await _read_request_body(request)))
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
        raise RehearsalError("invalid_request", 422) from None


def create_app(
    token: str,
    *,
    private_runtime: PrivateStudyRuntime,
    allowed_origin: str,
    notice_text: str,
) -> FastAPI:
    if not isinstance(token, str) or len(token) < 32 or any(char.isspace() for char in token):
        raise ValueError("invalid token")
    origin = urlsplit(allowed_origin)
    if (
        origin.scheme != "http"
        or origin.hostname != "localhost"
        or origin.port is None
        or origin.path
        or origin.query
        or origin.fragment
        or origin.username
        or origin.password
        or allowed_origin != f"http://localhost:{origin.port}"
    ):
        raise ValueError("use the canonical local rehearsal origin")
    if private_runtime.participant_store is None:
        raise ValueError("a private rehearsal store is required")
    if (
        not isinstance(notice_text, str)
        or hashlib.sha256(notice_text.encode("utf-8")).hexdigest()
        != private_runtime.participant_store.notice_sha256
    ):
        raise ValueError("notice content does not match the reviewed store release")
    tasks = json.loads((_HERE / "resources/rehearsal-tasks-v1.json").read_text(encoding="utf-8"))
    if (
        not isinstance(tasks, list)
        or len(tasks) != 3
        or any(
            not isinstance(task, dict)
            or set(task) != {"id", "title", "scenario"}
            or any(not isinstance(value, str) for value in task.values())
            for task in tasks
        )
    ):
        raise ValueError("invalid rehearsal task resource")
    sessions = RehearsalSessions(token, private_runtime.participant_store, notice_text, tasks)
    capacity = _Runtime(8)
    cookie = f"youth_law_rehearsal_{origin.port}"
    app = FastAPI(
        title="개발자 리허설",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=private_runtime.lifespan,
    )
    app.state.preparation_only = True
    app.state.private_study_runtime = private_runtime
    app.state.rehearsal_sessions = sessions

    @app.middleware("http")
    async def boundary(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = str(uuid4())
        started = time.monotonic()
        admitted = False
        path = request.url.path
        public = path in {"/", "/rehearsal", "/health"} or path.startswith("/static/")
        login = path == "/rehearsal/api/login" and request.method == "POST"
        try:
            if (
                not _is_loopback(request)
                or _has_forwarding_headers(request)
                or request.headers.get("host") != origin.netloc
            ):
                raise RehearsalError("not_found", 404)
            if request.method not in {"GET", "HEAD"}:
                if request.headers.get("origin") != allowed_origin:
                    raise RehearsalError("forbidden", 403)
                if (
                    request.headers.get("content-type", "").split(";", 1)[0].lower()
                    != "application/json"
                ):
                    raise RehearsalError("invalid_request", 422)
            if not public and not login:
                session = sessions.find(request.cookies.get(cookie))
                request.state.browser_session = session
                if request.method not in {"GET", "HEAD"} and path != "/api/preparation/card":
                    supplied = request.headers.get("x-rehearsal-csrf", "")
                    if not hmac.compare_digest(supplied.encode(), session.csrf_token.encode()):
                        raise RehearsalError("forbidden", 403)
            private_runtime.require_ready()
            if not capacity.admit():
                raise RehearsalError("busy", 429)
            admitted = True
            response = await call_next(request)
        except RehearsalError as exc:
            response = JSONResponse({"error": exc.code}, exc.status)
        except StudyStorageError as exc:
            response = JSONResponse({"error": exc.code}, _storage_status(exc.code))
        except StudyRuntimeUnavailable:
            response = JSONResponse({"error": "storage_unavailable"}, 503)
        except PreparationError as exc:
            response = JSONResponse({"error": exc.code}, exc.status_code)
        except TimeoutError:
            response = JSONResponse({"error": "request_timeout"}, 408)
        except Exception:
            # Never log request, exception text, policy text, IDs, cookie or credentials.
            response = JSONResponse({"error": "internal_error"}, 500)
        finally:
            if admitted:
                capacity.release()
        try:
            await asyncio.to_thread(
                private_runtime.record_request,
                {
                    "event": "protected_study_request",
                    "request_id": request_id,
                    "route": _route_name(path),
                    "status": response.status_code,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                },
            )
        except StudyRuntimeUnavailable:
            response = JSONResponse({"error": "storage_unavailable"}, 503)
        for key, value in _HEADERS.items():
            response.headers[key] = value
        # This app owns the final headers; the existing app boundaries are not installed.
        if path == "/rehearsal/card" and response.status_code == 200:
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
            response.headers["Content-Security-Policy"] = _HEADERS[
                "Content-Security-Policy"
            ].replace("frame-ancestors 'none'", "frame-ancestors 'self'")
        response.headers["X-Request-ID"] = request_id
        if response.status_code == 429:
            response.headers["Retry-After"] = "60" if login else "1"
        return response

    @app.get("/")
    def index() -> RedirectResponse:
        return RedirectResponse("/rehearsal")

    @app.get("/rehearsal")
    def page(request: Request) -> HTMLResponse:
        return _TEMPLATES.TemplateResponse(request, "rehearsal.html", {})

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ready",
            "mode": "developer_rehearsal",
            "session_admission": False,
            "application_input_storage": False,
            "external_model_enabled": False,
            "catalog": catalog_readiness(),
        }

    @app.post("/rehearsal/api/login")
    async def login(request: Request) -> JSONResponse:
        payload = await _payload(request, LoginInput)
        sid, session = await asyncio.to_thread(sessions.login, payload.token)
        try:
            state = await asyncio.to_thread(_state, session)
        except Exception:
            sessions.logout(sid)
            raise
        response = JSONResponse(state)
        response.set_cookie(
            cookie,
            sid,
            max_age=SESSION_SECONDS,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return response

    def _state(session: BrowserSession) -> dict:
        with sessions.locked(session):
            return sessions.state(session)

    @app.get("/rehearsal/api/state")
    async def state(request: Request) -> dict:
        return await asyncio.to_thread(_state, request.state.browser_session)

    @app.post("/rehearsal/api/logout")
    async def logout(request: Request) -> JSONResponse:
        await _payload(request, StrictInput)
        session = request.state.browser_session

        def remove() -> None:
            with sessions.locked(session):
                session.expires = 0
                sessions.logout(request.cookies.get(cookie, ""))

        await asyncio.to_thread(remove)
        response = JSONResponse({"logged_out": True})
        response.delete_cookie(cookie, path="/", httponly=True, samesite="strict")
        return response

    @app.post("/rehearsal/api/{action}")
    async def action(request: Request, action: str) -> dict:
        model = _ACTIONS.get(action)
        if model is None:
            raise RehearsalError("not_found", 404)
        payload = await _payload(request, model)
        return await asyncio.to_thread(
            sessions.apply, request.state.browser_session, action, payload
        )

    def card_access(session: BrowserSession) -> None:
        with sessions.locked(session):
            sessions.record(session)
            if not session.started:
                raise RehearsalError("task_not_started", 409)

    @app.get("/rehearsal/card")
    async def card_page(request: Request) -> HTMLResponse:
        await asyncio.to_thread(card_access, request.state.browser_session)
        return _TEMPLATES.TemplateResponse(request, "preparation.html", {"preparation_only": True})

    @app.get("/api/preparation/options")
    async def options(request: Request) -> JSONResponse:
        await asyncio.to_thread(card_access, request.state.browser_session)
        return await asyncio.to_thread(get_preparation_options)

    @app.post("/api/preparation/card")
    async def card(request: Request) -> JSONResponse:
        await asyncio.to_thread(card_access, request.state.browser_session)
        return await post_preparation_card(request)

    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
    return app
