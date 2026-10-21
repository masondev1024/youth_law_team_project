"""Isolated, stateless preparation-card app for fictional-case usability work.

Run locally: uvicorn app.study_api:app --host 127.0.0.1 --port 8011 --no-access-log
This entry point never imports the legal graph, dotenv, model clients or stores.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.preparation_boundary import install_preparation_boundary
from app.preparation_models import PreparationError
from app.preparation_routes import router
from app.preparation_service import catalog_readiness

if TYPE_CHECKING:
    from app.study_runtime import PrivateStudyRuntime

_HERE = Path(__file__).resolve().parent


def create_app(runtime: PrivateStudyRuntime | None = None) -> FastAPI:
    """Default remains stateless; explicit runtime stores only request metadata."""
    app = FastAPI(
        title="첫 금융 상담 준비",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=runtime.lifespan if runtime is not None else None,
    )
    app.state.preparation_only = True
    app.state.private_study_runtime = runtime
    install_preparation_boundary(
        app,
        preparation_only=True,
        log_sink=runtime.record_request if runtime is not None else None,
    )
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return RedirectResponse("/prepare")

    @app.get("/health")
    def health():
        if runtime is not None and not runtime.healthy:
            return JSONResponse({"status": "not_ready", "error": "storage_unavailable"}, 503)
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
        return {
            **state,
            "mode": "preparation_only",
            "external_model_enabled": False,
            "application_input_storage": False,
            "readiness_scope": "catalog_and_http_only",
            **(
                {
                    "private_metadata_logs": True,
                    "session_admission": False,
                    **(
                        {"participant_record_rehearsal": True}
                        if runtime.participant_store is not None
                        else {}
                    ),
                }
                if runtime is not None
                else {}
            ),
        }

    return app


app = create_app()
