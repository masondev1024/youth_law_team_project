"""Shared response and metadata boundary for preparation paths in either app."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

_LOGGER = logging.getLogger("youth_law.preparation")
if not _LOGGER.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    _LOGGER.addHandler(handler)
_LOGGER.setLevel(logging.INFO)
_LOG_PATHS = {"/", "/prepare", "/health", "/api/preparation/options", "/api/preparation/card"}
_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, noarchive",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'"
    ),
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def install_preparation_boundary(
    app: FastAPI, *, preparation_only: bool, log_sink: Callable[[dict], None] | None = None
) -> None:
    @app.middleware("http")
    async def preparation_boundary(request: Request, call_next):
        path = request.url.path
        if not preparation_only and path != "/prepare" and not path.startswith("/api/preparation/"):
            return await call_next(request)
        request_id = str(uuid4())
        started = time.monotonic()
        try:
            runtime = getattr(request.app.state, "private_study_runtime", None)
            if runtime is not None and not runtime.healthy:
                response = JSONResponse({"error": "storage_unavailable"}, status_code=503)
            else:
                response = await call_next(request)
        except Exception:
            # Exception messages may contain input; do not stringify them in logs.
            response = JSONResponse({"error": "internal_error"}, status_code=500)
        record = {
            "event": "preparation_request",
            "request_id": request_id,
            "route": path if path in _LOG_PATHS else "other",
            "status": response.status_code,
            "duration_ms": round((time.monotonic() - started) * 1000, 2),
        }
        if log_sink is None:
            _LOGGER.info(json.dumps(record))
        else:
            from app.study_runtime import StudyRuntimeUnavailable

            try:
                await asyncio.to_thread(log_sink, record)
            except StudyRuntimeUnavailable:
                response = JSONResponse({"error": "storage_unavailable"}, status_code=503)
        for key, value in _HEADERS.items():
            response.headers[key] = value
        response.headers["X-Request-ID"] = request_id
        return response
