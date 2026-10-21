"""A slow readiness read must not stall unrelated ASGI requests."""

from __future__ import annotations

import asyncio
import threading

import httpx
import pytest
from fastapi import FastAPI

from app.participant_routes import install_participant_routes
from app.study_runtime import StudyRuntimeUnavailable


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/ops/participant-records/status"),
        ("POST", "/ops/participant-records/consents"),
        ("PUT", "/ops/participant-records/example/observation"),
        ("DELETE", "/ops/participant-records/example"),
    ],
)
def test_slow_authority_gate_preserves_event_loop_and_fails_closed(method, path):
    entered = threading.Event()
    release = threading.Event()

    class SlowRuntime:
        participant_store = object()

        def require_ready(self):
            entered.set()
            release.wait(timeout=2)
            raise StudyRuntimeUnavailable("storage_unavailable")

    app = FastAPI()
    install_participant_routes(app, SlowRuntime())

    @app.get("/health-probe")
    async def health_probe():
        return {"alive": True}

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            request = asyncio.create_task(client.request(method, path))
            try:
                assert await asyncio.to_thread(entered.wait, 2), "gate was not reached"
                # No simulated network read has been released yet. This proves
                # health can execute while another request waits on the SDK.
                assert not request.done(), "readiness blocked the ASGI event loop"
                health = await asyncio.wait_for(client.get("/health-probe"), timeout=1)
                assert health.status_code == 200
                assert health.json() == {"alive": True}
            finally:
                release.set()
                response = await request
            assert response.status_code == 503
            assert response.json() == {"error": "storage_unavailable"}

    asyncio.run(exercise())
