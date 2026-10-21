"""Loopback-only, bearer-protected operator entry point for study preparation.

This is a local preparation/test boundary, not a participant API or a public
deployment security claim. The existing unauthenticated preview remains at
``app.study_api:app`` on port 8011.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import ipaddress
import json
import logging
import os
import stat
import time
from collections import Counter
from threading import Lock
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.preparation_boundary import _HEADERS
from app.preparation_models import PreparationError
from app.preparation_routes import get_preparation_options, post_preparation_card
from app.preparation_service import catalog_readiness

if TYPE_CHECKING:
    from app.study_runtime import PrivateStudyRuntime

_LOGGER = logging.getLogger("youth_law.preparation")
_MAX_ACTIVE_DEFAULT = 2
_ROUTES = {
    "/": "root",
    "/prepare": "prepare",
    "/health": "health",
    "/api/preparation/options": "options",
    "/api/preparation/card": "card",
    "/ops/metrics": "metrics",
    "/ops/observations": "observations",
    "/ops/retention": "retention",
    "/ops/participant-records/status": "participant_records",
    "/ops/participant-records/consents": "participant_records",
}
_STATUS_BUCKETS = {200, 201, 301, 302, 401, 403, 404, 408, 409, 410, 413, 422, 429, 500, 503}


class TokenFileError(ValueError):
    """Safe-to-report token-file validation failure."""


class _Runtime:
    def __init__(self, max_active: int) -> None:
        self.max_active = max_active
        self.lock = Lock()
        self.active = 0
        self.rejected = 0
        self.requests: Counter[tuple[str, str]] = Counter()

    def admit(self) -> bool:
        with self.lock:
            if self.active >= self.max_active:
                self.rejected += 1
                return False
            self.active += 1
            return True

    def release(self) -> None:
        with self.lock:
            self.active -= 1

    def record(self, route: str, status: int) -> None:
        status_label = str(status) if status in _STATUS_BUCKETS else "other"
        with self.lock:
            self.requests[(route, status_label)] += 1

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            rows = [
                {"route": route, "status": status, "count": count}
                for (route, status), count in sorted(self.requests.items())
            ]
            return {
                "scope": "this_process_only",
                "active": self.active,
                "rejected": self.rejected,
                "requests": rows,
            }


def _route_name(path: str) -> str:
    if path in _ROUTES:
        return _ROUTES[path]
    if path.startswith("/static/"):
        return "static"
    if path.startswith("/ops/observations/"):
        return "observations"
    if path.startswith("/ops/participant-records/"):
        return "participant_records"
    return "other"


def _is_loopback(request: Request) -> bool:
    client = request.client
    if client is None:
        return False
    try:
        return ipaddress.ip_address(client.host).is_loopback
    except ValueError:
        return False


def _has_forwarding_headers(request: Request) -> bool:
    return any(
        name.lower() == b"forwarded" or name.lower().startswith(b"x-forwarded-")
        for name, _ in request.scope.get("headers", [])
    )


def _safe_exception_class(exc: Exception) -> str:
    for exception_type in (RuntimeError, ValueError, TypeError, OSError):
        if type(exc) is exception_type:
            return exception_type.__name__
    return "UnexpectedError"


def create_app(
    token: str,
    max_active: int = _MAX_ACTIVE_DEFAULT,
    *,
    private_runtime: PrivateStudyRuntime | None = None,
) -> FastAPI:
    """Create an isolated protected app with bounded synchronous work."""
    if not isinstance(token, str) or len(token) < 32 or any(ch.isspace() for ch in token):
        raise ValueError("token must be at least 32 non-whitespace characters")
    if not isinstance(max_active, int) or isinstance(max_active, bool) or max_active < 1:
        raise ValueError("max_active must be a positive integer")

    app = FastAPI(
        title="Protected study preparation",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=private_runtime.lifespan if private_runtime is not None else None,
    )
    app.state.preparation_only = True
    app.state.private_study_runtime = private_runtime
    runtime = _Runtime(max_active)
    app.state.protected_study_runtime = runtime
    expected = token.encode("utf-8")

    @app.middleware("http")
    async def protected_boundary(request: Request, call_next):
        request_id = str(uuid4())
        started = time.monotonic()
        route = _route_name(request.url.path)
        response = None
        admitted = False
        exception_class = None
        try:
            if not _is_loopback(request) or _has_forwarding_headers(request):
                response = JSONResponse({"error": "not_found"}, status_code=404)
            else:
                scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
                authorized = scheme.lower() == "bearer" and hmac.compare_digest(
                    supplied.encode("utf-8"), expected
                )
                if not authorized:
                    response = JSONResponse({"error": "unauthorized"}, status_code=401)
                    response.headers["WWW-Authenticate"] = "Bearer"
                elif route == "metrics" and request.method == "GET":
                    response = JSONResponse(runtime.snapshot())
                elif (
                    route == "retention" and request.method == "GET" and private_runtime is not None
                ):
                    response = JSONResponse(
                        private_runtime.snapshot(), 200 if private_runtime.healthy else 503
                    )
                elif private_runtime is not None and not private_runtime.healthy:
                    response = JSONResponse({"error": "storage_unavailable"}, 503)
                elif not runtime.admit():
                    response = JSONResponse({"error": "busy"}, status_code=429)
                    response.headers["Retry-After"] = "1"
                else:
                    admitted = True
                    response = await call_next(request)
        except Exception as exc:
            # Never stringify exceptions: request data can appear in exception text.
            exception_class = _safe_exception_class(exc)
            response = JSONResponse({"error": "internal_error"}, status_code=500)
        finally:
            if admitted:
                runtime.release()

        assert response is not None
        log_record = {
            "event": "protected_study_request",
            "request_id": request_id,
            "route": route,
            "status": response.status_code,
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
        }
        if exception_class is not None:
            log_record["exception_class"] = exception_class
        if private_runtime is None:
            _LOGGER.info(json.dumps(log_record))
        else:
            from app.study_runtime import StudyRuntimeUnavailable

            try:
                await asyncio.to_thread(private_runtime.record_request, log_record)
            except StudyRuntimeUnavailable:
                response = JSONResponse(
                    private_runtime.snapshot()
                    if route == "retention"
                    else {"error": "storage_unavailable"},
                    status_code=503,
                )
        runtime.record(route, response.status_code)
        for key, value in _HEADERS.items():
            response.headers[key] = value
        response.headers["X-Request-ID"] = request_id
        return response

    @app.get("/health")
    def health() -> JSONResponse:
        try:
            state = catalog_readiness()
        except PreparationError:
            return JSONResponse(
                {
                    "status": "not_ready",
                    "mode": "preparation_only",
                    "error": "source_unavailable",
                },
                status_code=503,
            )
        return JSONResponse(
            {
                **state,
                "mode": "preparation_only",
                "external_model_enabled": False,
                "application_input_storage": False,
                "readiness_scope": "catalog_and_http_only",
                **(
                    {
                        "private_metadata_logs": True,
                        "synthetic_observation_storage": True,
                        "session_admission": False,
                    }
                    if private_runtime is not None
                    else {}
                ),
            }
        )

    app.add_api_route("/api/preparation/options", get_preparation_options, methods=["GET"])
    app.add_api_route("/api/preparation/card", post_preparation_card, methods=["POST"])

    if private_runtime is not None:
        _install_observation_routes(app, private_runtime)
        if private_runtime.participant_store is not None:
            from app.participant_routes import install_participant_routes

            install_participant_routes(app, private_runtime)

    return app


def _install_observation_routes(app: FastAPI, runtime: PrivateStudyRuntime) -> None:
    from app.preparation_routes import _read_request_body
    from app.study_runtime import StudyRuntimeUnavailable
    from app.study_storage import StudyStorageError

    def storage_response(error: StudyStorageError) -> JSONResponse:
        status = {"invalid_record": 422, "conflict": 409, "expired": 410}.get(error.code, 503)
        if status == 503:
            runtime.healthy = False
        return JSONResponse(
            {"error": error.code if status != 503 else "storage_unavailable"}, status
        )

    @app.post("/ops/observations")
    async def observation(request: Request):
        try:
            await asyncio.to_thread(runtime.require_ready)
            key = request.headers.get("Idempotency-Key")
            try:
                parsed_key = UUID(key) if key is not None else None
            except ValueError:
                parsed_key = None
            if parsed_key is None or parsed_key.version != 4 or str(parsed_key) != key:
                return JSONResponse({"error": "invalid_record"}, 422)
            payload = json.loads(await _read_request_body(request))
            record = await asyncio.to_thread(
                runtime.store.write_observation,
                payload,
                record_id=key,
            )
            return JSONResponse(
                {
                    "record_id": record["id"],
                    "expires_at": record["expires_at"],
                    "synthetic_only": True,
                    "session_admission": False,
                },
                status_code=201,
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"error": "invalid_record"}, 422)
        except PreparationError as exc:
            return JSONResponse({"error": exc.code}, exc.status_code)
        except TimeoutError:
            return JSONResponse({"error": "request_timeout"}, 408)
        except StudyStorageError as exc:
            return storage_response(exc)
        except StudyRuntimeUnavailable:
            return JSONResponse({"error": "storage_unavailable"}, 503)

    @app.delete("/ops/observations/{record_id}")
    async def withdraw(record_id: str):
        try:
            await asyncio.to_thread(runtime.require_ready)
            deleted = await asyncio.to_thread(runtime.store.delete_observation, record_id)
            return {"deleted": deleted, "synthetic_only": True}
        except StudyStorageError as exc:
            return storage_response(exc)
        except StudyRuntimeUnavailable:
            return JSONResponse({"error": "storage_unavailable"}, 503)


def read_token_file(path: str | os.PathLike[str]) -> str:
    """Read a current-user-owned, non-symlink 0600 token file safely."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(os.fspath(path), flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise TokenFileError
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise TokenFileError
            if info.st_size > 4096:
                raise TokenFileError
            with os.fdopen(fd, "rb", closefd=False) as token_file:
                raw = token_file.read(4097)
        finally:
            os.close(fd)
    except (OSError, ValueError) as exc:
        raise TokenFileError from exc
    if len(raw) > 4096:
        raise TokenFileError
    try:
        token = raw.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError as exc:
        raise TokenFileError from exc
    if len(token) < 32 or any(ch.isspace() for ch in token):
        raise TokenFileError
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run protected local study preparation")
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--port", type=int, default=8013)
    parser.add_argument("--max-active", type=int, default=_MAX_ACTIVE_DEFAULT)
    args = parser.parse_args(argv)
    try:
        token = read_token_file(args.token_file)
        app = create_app(token, args.max_active)
        if not 1 <= args.port <= 65535:
            raise ValueError
    except (TokenFileError, ValueError):
        print("protected study startup failed", file=__import__("sys").stderr)
        return 2

    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        workers=1,
        access_log=False,
        proxy_headers=False,
        forwarded_allow_ips="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
