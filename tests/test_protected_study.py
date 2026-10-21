"""Local-only security and capacity tests for the protected study entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app import preparation_service
from app.preparation_models import PreparationError
from app.protected_study import TokenFileError, create_app, main, read_token_file
from app.study_api import app as preview_app

TOKEN = "local-test-token-with-at-least-32-characters"


def local_client(app):
    return TestClient(app, client=("127.0.0.1", 50000))


def test_factory_validates_token_and_active_limit():
    for token in ("short", "a" * 32 + " "):
        try:
            create_app(token)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid token accepted")
    for limit in (0, -1, True):
        try:
            create_app(TOKEN, limit)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid active limit accepted")


def test_auth_covers_page_apis_and_metrics_and_does_not_leak_markers(caplog):
    secret = "NEVER_ECHO_TOKEN_OR_BODY_OR_QUERY_0123456789"
    app = create_app(secret)
    caplog.set_level(logging.INFO, logger="youth_law.preparation")
    with local_client(app) as client:
        for path in ("/prepare", "/health", "/api/preparation/options", "/ops/metrics"):
            denied = client.get(f"{path}?marker={secret}")
            assert denied.status_code == 401
            assert denied.json() == {"error": "unauthorized"}
            assert denied.headers["cache-control"].startswith("no-store")
            assert denied.headers["x-request-id"]
        denied_post = client.post(
            f"/api/preparation/card?marker={secret}",
            content=f'{{"question":"{secret}"}}',
            headers={"Authorization": f"Bearer wrong-{secret}"},
        )
        assert denied_post.status_code == 401
        assert secret not in denied_post.text
        assert client.get(f"/health?token={secret}").status_code == 401
        assert client.post(
            "/api/preparation/card", json={"token": secret}
        ).status_code == 401
        assert secret not in "\n".join(record.getMessage() for record in caplog.records)

        response = client.get("/ops/metrics", headers={"Authorization": f"Bearer {secret}"})
        assert response.status_code == 200
        assert response.json()["scope"] == "this_process_only"
        assert response.headers["cache-control"].startswith("no-store")


def test_actual_remote_client_and_forwarding_headers_are_rejected():
    app = create_app(TOKEN)
    remote = TestClient(app, client=("198.51.100.20", 40000))
    denied = remote.get("/health", headers={"Authorization": f"Bearer {TOKEN}"})
    assert denied.status_code == 404
    assert denied.headers["x-request-id"]

    with local_client(app) as client:
        for header in ("Forwarded", "X-Forwarded-For", "X-Forwarded-Proto", "x-forwarded-host"):
            response = client.get("/health", headers={
                "Authorization": f"Bearer {TOKEN}", header: "127.0.0.1",
            })
            assert response.status_code == 404
            assert response.json() == {"error": "not_found"}


def test_valid_token_does_not_enable_ui_or_static_serving():
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with local_client(create_app(TOKEN)) as client:
        assert client.get("/", headers=headers).status_code == 404
        assert client.get("/prepare", headers=headers).status_code == 404
        assert client.get("/static/preparation.js", headers=headers).status_code == 404


def test_health_matches_preview_digest_and_catalog_failure_recovers(monkeypatch):
    monkeypatch.setattr(preparation_service, "_today", lambda: date(2026, 10, 20))
    expected_catalog = preparation_service.catalog_readiness()
    with TestClient(preview_app) as preview, local_client(create_app(TOKEN)) as protected:
        preview_health = preview.get("/health")
        protected_health = protected.get("/health", headers={"Authorization": f"Bearer {TOKEN}"})
        preview_digest = preview_health.json().get("catalog_sha256")
        protected_digest = protected_health.json().get("catalog_sha256")
        expected_digest = expected_catalog.get("catalog_sha256")
        assert preview_health.status_code == protected_health.status_code == 200
        assert re.fullmatch(r"[0-9a-f]{64}", preview_digest or "")
        assert preview_digest == protected_digest == expected_digest

        def fail():
            raise PreparationError(503, "source_unavailable")

        monkeypatch.setattr("app.protected_study.catalog_readiness", fail)
        unavailable = protected.get("/health", headers={"Authorization": f"Bearer {TOKEN}"})
        assert unavailable.status_code == 503
        assert unavailable.json() == {
            "status": "not_ready", "mode": "preparation_only", "error": "source_unavailable",
        }
        monkeypatch.setattr(
            "app.protected_study.catalog_readiness", preparation_service.catalog_readiness
        )
        recovered = protected.get("/health", headers={"Authorization": f"Bearer {TOKEN}"})
        assert recovered.status_code == 200
        assert recovered.json().get("catalog_sha256") == expected_digest


def test_legacy_routes_stay_missing_and_metrics_require_auth():
    with local_client(create_app(TOKEN)) as client:
        assert client.get("/ops/metrics").status_code == 401
        metrics_post = client.post(
            "/ops/metrics", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert metrics_post.status_code == 404
        for path in ("/api/consult", "/api/draft", "/api/session/example"):
            response = client.post(
                path, json={"question": "fictional"},
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            assert response.status_code == 404
            assert response.headers["x-request-id"]
            assert response.headers["cache-control"].startswith("no-store")


def test_admission_limit_rejects_without_queue_and_releases_after_error(monkeypatch):
    started = Event()
    release = Event()

    def controlled(_payload):
        started.set()
        assert release.wait(5), "controlled service fixture timed out"
        raise RuntimeError("private failure")

    monkeypatch.setattr(preparation_service, "build_card", controlled)
    app = create_app(TOKEN, max_active=1)
    client = local_client(app)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    body = {"issue": "repayment", "selected_facts": []}
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, "/api/preparation/card", json=body, headers=headers)
        assert started.wait(3)
        rejected = client.post("/api/preparation/card", json=body, headers=headers)
        assert rejected.status_code == 429
        assert rejected.headers["retry-after"] == "1"
        release.set()
        failed = first.result(timeout=5)
    assert failed.status_code == 500
    assert "private failure" not in failed.text

    monkeypatch.setattr(preparation_service, "build_card", lambda _payload: {"card": "fixture"})
    recovered = client.post("/api/preparation/card", json=body, headers=headers)
    assert recovered.status_code == 200
    assert recovered.json() == {"card": "fixture"}
    metrics = client.get("/ops/metrics", headers=headers).json()
    assert metrics["active"] == 0
    assert metrics["rejected"] == 1
    client.close()


def test_async_requests_remain_responsive_while_card_work_is_offloaded(monkeypatch, caplog):
    async def scenario():
        started = Event()
        release = Event()
        private_error = "PRIVATE_EXCEPTION_TEXT_12345"

        def controlled(_payload):
            started.set()
            assert release.wait(5), "controlled service fixture timed out"
            raise RuntimeError(private_error)

        monkeypatch.setattr(preparation_service, "build_card", controlled)
        app = create_app(TOKEN, max_active=1)
        transport = ASGITransport(app=app, client=("127.0.0.1", 50000))
        headers = {"Authorization": f"Bearer {TOKEN}"}
        body = {"issue": "repayment", "selected_facts": []}
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            first = asyncio.create_task(
                client.post("/api/preparation/card", json=body, headers=headers)
            )
            try:
                assert await asyncio.to_thread(started.wait, 3)
                health = await client.get("/health", headers=headers)
                assert health.status_code == 429
                assert health.headers["retry-after"] == "1"
                metrics = await client.get("/ops/metrics", headers=headers)
                assert metrics.status_code == 200
                assert metrics.json()["active"] == 1
            finally:
                release.set()

            failed = await first
            assert failed.status_code == 500
            assert private_error not in failed.text
            after_failure = await client.get("/ops/metrics", headers=headers)
            assert after_failure.json()["active"] == 0
            assert after_failure.json()["rejected"] == 1

            monkeypatch.setattr(
                preparation_service, "build_card", lambda _payload: {"card": "fixture"}
            )
            recovered = await client.post(
                "/api/preparation/card", json=body, headers=headers
            )
            assert recovered.status_code == 200
            assert recovered.json() == {"card": "fixture"}

        records = [record.getMessage() for record in caplog.records]
        assert all(private_error not in record for record in records)
        assert any('"exception_class": "RuntimeError"' in record for record in records)

    caplog.set_level(logging.INFO, logger="youth_law.preparation")
    asyncio.run(scenario())


def test_token_file_requires_current_user_regular_private_file_and_cli_is_loopback(
    tmp_path, monkeypatch, capsys
):
    token_path = tmp_path / "operator.token"
    token_path.write_text(TOKEN + "\n", encoding="utf-8")
    token_path.chmod(0o600)
    assert read_token_file(token_path) == TOKEN

    insecure_path = tmp_path / "insecure.token"
    insecure_path.write_text(TOKEN, encoding="utf-8")
    insecure_path.chmod(0o644)
    try:
        read_token_file(insecure_path)
    except TokenFileError:
        pass
    else:
        raise AssertionError("group-readable token file accepted")

    symlink_path = tmp_path / "linked.token"
    symlink_path.symlink_to(token_path)
    try:
        read_token_file(symlink_path)
    except TokenFileError:
        pass
    else:
        raise AssertionError("symlink token file accepted")

    import uvicorn

    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(app=app, **kwargs))
    assert main(["--token-file", str(token_path), "--port", "8013", "--max-active", "2"]) == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8013
    assert captured["workers"] == 1
    assert captured["access_log"] is False
    assert captured["proxy_headers"] is False
    assert captured["forwarded_allow_ips"] == ""

    missing = tmp_path / "missing-secret-path.token"
    assert main(["--token-file", str(missing)]) == 2
    error = capsys.readouterr().err
    assert str(missing) not in error
    assert TOKEN not in error


def test_import_path_has_no_legacy_model_storage_dotenv_or_tracing_imports():
    code = """
import sys
from app.protected_study import create_app
create_app('local-test-token-with-at-least-32-characters')
assert not any(name in sys.modules for name in (
    'config', 'graph', 'app.service', 'common.llm', 'common.db',
    'common.logging_store', 'app.session_store', 'boto3', 'langsmith', 'dotenv'
))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
