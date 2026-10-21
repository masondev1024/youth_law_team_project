"""Operator-only rehearsal of the participant consent and observation contract."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.participant_models import NOTICE_VERSION, POLICY_VERSION, ConsentInput, ObservationInput
from app.preparation_models import PreparationError
from app.preparation_routes import _read_request_body
from app.study_runtime import StudyRuntimeUnavailable
from app.study_storage import StudyStorageError

if TYPE_CHECKING:
    from app.study_runtime import PrivateStudyRuntime


class ConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    task_id: str
    consent: ConsentInput


class ObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(ge=1)
    observation: ObservationInput


def _result(record: dict) -> dict:
    return {
        "record_id": record["record_id"],
        "expires_at": record["expires_at"],
        "version": record["version"],
        "record_origin": "developer_rehearsal",
        "session_admission": False,
    }


def install_participant_routes(app: FastAPI, runtime: PrivateStudyRuntime) -> None:
    store = runtime.participant_store
    if store is None:
        raise ValueError("participant storage is required")

    def failure(exc: Exception) -> JSONResponse:
        if isinstance(exc, StudyStorageError):
            status = {
                "invalid_record": 422,
                "invalid_request_key": 422,
                "conflict": 409,
                "revoked": 409,
                "expired": 410,
                "admission_closed": 403,
            }.get(exc.code, 503)
            if status == 503:
                runtime.healthy = False
            return JSONResponse(
                {"error": exc.code if status != 503 else "storage_unavailable"}, status
            )
        if isinstance(exc, PreparationError):
            return JSONResponse({"error": exc.code}, exc.status_code)
        if isinstance(exc, TimeoutError):
            return JSONResponse({"error": "request_timeout"}, 408)
        if isinstance(exc, StudyRuntimeUnavailable):
            return JSONResponse({"error": "storage_unavailable"}, 503)
        return JSONResponse({"error": "invalid_record"}, 422)

    @app.get("/ops/participant-records/status")
    async def status() -> JSONResponse:
        try:
            runtime.require_ready()
            counts = await asyncio.to_thread(store.summary)
            return JSONResponse(
                {
                    "session_admission": False,
                    "record_origin": "developer_rehearsal",
                    "notice_version": NOTICE_VERSION,
                    "policy_version": POLICY_VERSION,
                    "counts": counts,
                }
            )
        except (StudyStorageError, StudyRuntimeUnavailable) as exc:
            return failure(exc)

    @app.post("/ops/participant-records/consents")
    async def consent(request: Request) -> JSONResponse:
        try:
            runtime.require_ready()
            payload = ConsentRequest.model_validate(json.loads(await _read_request_body(request)))
            record = await asyncio.to_thread(
                store.create_consent,
                payload.consent.model_dump(),
                task_id=payload.task_id,
                request_key=request.headers.get("Idempotency-Key", ""),
            )
            if record is None:
                return JSONResponse({"status": "declined", "record_created": False})
            return JSONResponse(_result(record), 201)
        except (
            ValidationError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            PreparationError,
            TimeoutError,
            StudyStorageError,
            StudyRuntimeUnavailable,
        ) as exc:
            return failure(exc)

    @app.put("/ops/participant-records/{record_id}/observation")
    async def observation(record_id: str, request: Request) -> JSONResponse:
        try:
            runtime.require_ready()
            payload = ObservationRequest.model_validate(
                json.loads(await _read_request_body(request))
            )
            record = await asyncio.to_thread(
                store.write_observation,
                record_id,
                payload.observation.model_dump(),
                expected_version=payload.expected_version,
            )
            return JSONResponse(_result(record))
        except (
            ValidationError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            PreparationError,
            TimeoutError,
            StudyStorageError,
            StudyRuntimeUnavailable,
        ) as exc:
            return failure(exc)

    @app.delete("/ops/participant-records/{record_id}")
    async def withdraw(record_id: str) -> JSONResponse:
        try:
            runtime.require_ready()
            removed = await asyncio.to_thread(store.withdraw, record_id)
            return JSONResponse({"deleted": removed, "record_origin": "developer_rehearsal"})
        except (StudyStorageError, StudyRuntimeUnavailable) as exc:
            return failure(exc)
