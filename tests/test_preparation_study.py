"""Study-entry boundaries: no legacy inference, no retained input, safe failures."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app import preparation_service
from app.preparation_models import PreparationError
from app.study_api import app


def test_study_entry_excludes_model_and_storage_even_with_legacy_flags():
    code = """
import sys
from app.study_api import app
from fastapi.testclient import TestClient
with TestClient(app) as client:
    assert client.get('/prepare').status_code == 200
    assert client.get('/health').status_code == 200
assert not any(name in sys.modules for name in (
    'config', 'graph', 'app.service', 'common.llm', 'common.db',
    'common.logging_store', 'app.session_store', 'boto3', 'langsmith', 'dotenv'
))
print('isolated')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env={
            "PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1",
            "LANGCHAIN_TRACING_V2": "true", "LANGSMITH_TRACING": "true",
            "ENABLE_RDS_LOGGING": "true", "DATABASE_URL": "not-a-real-database",
        },
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "isolated"


def test_study_blocks_legacy_paths_and_sets_security_headers():
    with TestClient(app) as client:
        page = client.get("/prepare")
        assert page.status_code == 200
        assert 'href="/"' not in page.text
        assert page.headers["cache-control"].startswith("no-store")
        assert page.headers["content-security-policy"].startswith("default-src 'self'")
        assert page.headers["x-content-type-options"] == "nosniff"
        for path in ("/api/consult", "/api/draft", "/api/session/example"):
            response = client.post(path, json={"question": "fictional"})
            assert response.status_code == 404
            assert response.headers["cache-control"].startswith("no-store")
            assert response.headers.get("x-request-id")


def test_metadata_log_and_error_do_not_echo_input_or_url_query(caplog):
    marker = "FICTIONAL_INPUT_MUST_NOT_BE_RECORDED"
    caplog.set_level(logging.INFO, logger="youth_law.preparation")
    with TestClient(app) as client:
        response = client.post(
            f"/api/preparation/card?unused={marker}",
            json={"issue": "repayment", "selected_facts": [], "question": marker},
            headers={"X-Request-ID": marker},
        )
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_request"}
    assert marker not in response.text
    assert response.headers["x-request-id"] != marker
    records = [r.getMessage() for r in caplog.records if r.name == "youth_law.preparation"]
    assert records and all(marker not in text for text in records)
    row = json.loads(records[-1])
    assert set(row) == {"event", "request_id", "route", "status", "duration_ms"}


def test_source_failure_is_not_a_healthy_or_generated_response(monkeypatch):
    def fail():
        raise PreparationError(503, "source_unavailable")

    monkeypatch.setattr("app.study_api.catalog_readiness", fail)
    monkeypatch.setattr(preparation_service, "_load_catalog", fail)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 503
        response = client.post(
            "/api/preparation/card", json={"issue": "repayment", "selected_facts": []}
        )
    assert response.status_code == 503
    assert response.json() == {"error": "source_unavailable"}
    assert "official_route" not in response.json()


def test_unexpected_failure_is_sanitized_and_correlated(monkeypatch):
    def fail(_payload):
        raise RuntimeError("FICTIONAL_PRIVATE_ERROR")

    monkeypatch.setattr(preparation_service, "build_card", fail)
    with TestClient(app) as client:
        response = client.post(
            "/api/preparation/card", json={"issue": "repayment", "selected_facts": []}
        )
    assert response.status_code == 500
    assert response.json() == {"error": "internal_error"}
    assert "FICTIONAL_PRIVATE_ERROR" not in response.text
    assert response.headers["x-request-id"]


def test_existing_consult_contract_stays_independent(monkeypatch):
    from app.api import app as legal_app

    original_payload = {
        "question": "fictional", "rewritten_question": None,
        "domains": [{"id": "finance", "name": "금융"}], "in_scope": True,
        "final_answer": "original answer fixture", "answer_blocks": [],
        "verification_report": [],
    }
    monkeypatch.setattr("app.api.service.consult", lambda question, session_id: original_payload)
    client = TestClient(legal_app)  # No context: do not invoke original embedding startup.
    assert client.post("/api/consult", json={"question": "fictional"}).json() == original_payload
    assert 'href="/prepare"' in client.get("/").text
    assert 'href="/"' in client.get("/prepare").text
    for path in ("/prepare", "/api/preparation/options"):
        response = client.get(path)
        assert response.headers["x-request-id"]
        assert response.headers["cache-control"].startswith("no-store")
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    response = client.post("/api/preparation/card", json={"question": "fictional"})
    assert response.status_code == 422
    assert response.headers["x-request-id"]
