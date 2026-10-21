"""HTTP boundary for the standalone preparation-card experience."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from app import preparation_service
from app.preparation_models import PreparationError

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")
_MAX_REQUEST_BYTES = 8192
_BODY_READ_TIMEOUT_SECONDS = 5


def _json_response(content: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        content,
        status_code=status_code,
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


async def _read_request_body(request: Request) -> bytes:
    chunks: list[bytes] = []
    total_bytes = 0
    async with asyncio.timeout(_BODY_READ_TIMEOUT_SECONDS):
        async for chunk in request.stream():
            total_bytes += len(chunk)
            if total_bytes > _MAX_REQUEST_BYTES:
                raise PreparationError(413, "invalid_request")
            chunks.append(chunk)
    return b"".join(chunks)


@router.get("/api/preparation/options")
def get_preparation_options() -> JSONResponse:
    try:
        return _json_response(preparation_service.options())
    except PreparationError as exc:
        return _json_response({"error": exc.code}, exc.status_code)


@router.post("/api/preparation/card")
async def post_preparation_card(request: Request) -> JSONResponse:
    try:
        raw_body = await _read_request_body(request)
        payload = json.loads(raw_body)
        # File reading and full catalog validation must not block the ASGI loop.
        card = await run_in_threadpool(preparation_service.build_card, payload)
        return _json_response(card)
    except TimeoutError:
        return _json_response({"error": "request_timeout"}, 408)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _json_response({"error": "invalid_request"}, 422)
    except PreparationError as exc:
        return _json_response({"error": exc.code}, exc.status_code)


@router.get("/prepare", response_class=HTMLResponse)
def preparation_page(request: Request) -> HTMLResponse:
    response = _TEMPLATES.TemplateResponse(
        request,
        "preparation.html",
        {"preparation_only": getattr(request.app.state, "preparation_only", True)},
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response
