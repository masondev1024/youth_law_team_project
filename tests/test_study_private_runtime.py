"""App-level checks for the private synthetic-study storage lifecycle."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest
from fastapi.testclient import TestClient

from app.protected_study import create_app as create_protected_app
from app.study_api import create_app as create_preview_app
from app.study_runtime import PrivateServerLogHandler, PrivateStudyRuntime
from app.study_storage import PrivateStudyStore

TOKEN = "private-runtime-integration-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def private_root(tmp_path):
    root = tmp_path / "private-study"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    marker = root / ".study-storage.json"
    marker.write_text(
        json.dumps(
            {
                "schema": "youth-law-private-storage.v1",
                "owner_uid": os.getuid(),
                "real_participant_processing_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(marker, 0o600)
    return root


def make_runtime(private_root, clock: Clock) -> PrivateStudyRuntime:
    return PrivateStudyRuntime(PrivateStudyStore(private_root, clock=clock), interval_seconds=0.03)


def observation(**updates):
    value = {
        "scenario": "synthetic",
        "consent": "simulated_granted",
        "task_id": "formative-ux-repayment-01",
        "completion": "completed",
        "elapsed_seconds": 12,
    }
    value.update(updates)
    return value


def wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_http_observation_idempotency_and_retention_logs_expire_at_ttl(private_root):
    clock = Clock()
    runtime = make_runtime(private_root, clock)
    app = create_protected_app(TOKEN, private_runtime=runtime)
    record_id = str(uuid.uuid4())

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        created = client.post(
            "/ops/observations",
            json=observation(),
            headers={
                **AUTH,
                "Idempotency-Key": record_id,
            },
        )
        assert created.status_code == 201
        assert created.json() == {
            "record_id": record_id,
            "expires_at": "2026-11-19T12:00:00.000000Z",
            "synthetic_only": True,
            "session_admission": False,
        }

        duplicate = client.post(
            "/ops/observations",
            json=observation(),
            headers={
                **AUTH,
                "Idempotency-Key": record_id,
            },
        )
        assert duplicate.status_code == 201
        assert duplicate.json()["record_id"] == record_id
        assert duplicate.json()["expires_at"] == created.json()["expires_at"]

        conflict = client.post(
            "/ops/observations",
            json=observation(elapsed_seconds=13),
            headers={
                **AUTH,
                "Idempotency-Key": record_id,
            },
        )
        assert conflict.status_code == 409

        before = runtime.cleanup_runs
        clock.value += timedelta(hours=24)
        assert wait_for(
            lambda: runtime.cleanup_runs > before and runtime.store.summary()["logs"] == 0
        )
        retention = client.get("/ops/retention", headers=AUTH)
        assert retention.status_code == 200
        state = retention.json()
        assert state["session_admission"] is False
        assert state["log_retention_hours"] == 24
        assert state["observation_retention_days"] == 30
        assert state["cleanup_while_app_stopped"] is False
        assert runtime.store.summary()["observations"] == 1
        assert runtime.store.summary()["logs"] == 1  # The retention read is newly logged.

        before = runtime.cleanup_runs
        clock.value += timedelta(days=30) - timedelta(hours=24)
        assert wait_for(
            lambda: runtime.cleanup_runs > before and runtime.store.summary()["observations"] == 0
        )
        retention = client.get("/ops/retention", headers=AUTH)
        assert retention.status_code == 200
        assert runtime.store.summary()["observations"] == 0
        assert runtime.store.summary()["logs"] == 1


def test_startup_sweep_catches_up_after_app_was_stopped(private_root):
    clock = Clock()
    first_runtime = make_runtime(private_root, clock)
    first_app = create_protected_app(TOKEN, private_runtime=first_runtime)
    with TestClient(first_app, client=("127.0.0.1", 50000)) as client:
        created = client.post(
            "/ops/observations",
            json=observation(),
            headers={**AUTH, "Idempotency-Key": str(uuid.uuid4())},
        )
        assert created.status_code == 201
        assert first_runtime.store.summary()["logs"] >= 1

    # Cleanup is explicitly app-owned: elapsed wall clock alone does not purge.
    clock.value += timedelta(days=31)
    assert list(first_runtime.store.observations.glob("*.json"))
    second_runtime = make_runtime(private_root, clock)
    second_app = create_protected_app(TOKEN, private_runtime=second_runtime)
    with TestClient(second_app, client=("127.0.0.1", 50000)) as client:
        state = client.get("/ops/retention", headers=AUTH)
        assert state.status_code == 200
        assert state.json()["cleanup_runs"] >= 1
        assert second_runtime.store.summary()["observations"] == 0
        assert second_runtime.store.summary()["logs"] == 1  # Newly logged retention read.


def test_failed_startup_purge_is_visible_and_periodic_retry_recovers(private_root, monkeypatch):
    clock = Clock()
    store = PrivateStudyStore(private_root, clock=clock)
    store.write_observation(observation())
    clock.value += timedelta(days=31)
    original_purge = store.purge_expired
    attempts = 0

    def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("private failure marker")
        retry_started.set()
        assert allow_retry.wait(2), "test did not release the periodic retry"
        return original_purge()

    retry_started = Event()
    allow_retry = Event()
    monkeypatch.setattr(store, "purge_expired", fail_once)
    runtime = PrivateStudyRuntime(store, interval_seconds=0.03)
    app = create_protected_app(TOKEN, private_runtime=runtime)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        unavailable = client.get("/ops/retention", headers=AUTH)
        assert unavailable.status_code == 503
        assert unavailable.json()["state"] == "storage_unavailable"
        assert unavailable.json()["cleanup_failures"] == 1

        assert retry_started.wait(2)
        allow_retry.set()
        assert wait_for(lambda: runtime.healthy and runtime.cleanup_runs >= 1)
        recovered = client.get("/ops/retention", headers=AUTH)
        assert recovered.status_code == 200
        assert recovered.json()["cleanup_failures"] == 1
        assert runtime.store.summary()["observations"] == 0
        assert attempts < 10  # Retry cadence remains bounded by the configured interval.


def test_private_preview_logs_only_to_canonical_runtime_files(private_root, caplog):
    clock = Clock()
    runtime = make_runtime(private_root, clock)
    app = create_preview_app(runtime)
    caplog.set_level(logging.INFO, logger="youth_law.preparation")

    with TestClient(app) as client:
        assert client.get("/prepare").status_code == 200
        assert client.get("/health").status_code == 200
        assert client.get("/ops/retention").status_code == 404
        assert client.post("/ops/observations", json=observation()).status_code == 404

    assert not [r for r in caplog.records if r.name == "youth_law.preparation"]
    log_paths = list((private_root / "app-runtime-v1" / "logs").glob("*.json"))
    assert len(log_paths) == 4
    for path in log_paths:
        assert path.parent == runtime.store.logs
        assert os.stat(path).st_mode & 0o777 == 0o600
        payload = json.loads(path.read_text(encoding="utf-8"))["payload"]
        assert payload["event"] == "preparation_request"
        assert payload["route"] in {"/prepare", "/health", "other"}
    assert not list(runtime.store.observations.glob("*.json"))


def test_protected_private_api_rejects_unauthorized_real_or_freeform_writes(private_root, caplog):
    clock = Clock()
    runtime = make_runtime(private_root, clock)
    app = create_protected_app(TOKEN, private_runtime=runtime)
    marker = "PRIVATE_BODY_QUERY_TOKEN_MARKER_123456789"
    caplog.set_level(logging.INFO, logger="youth_law.preparation")

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        early = client.post(
            "/ops/observations",
            json=observation(),
            headers={
                **AUTH,
                "Idempotency-Key": str(uuid.uuid4()),
            },
        )
        assert early.status_code == 201
        withdrawn = client.delete(
            f"/ops/observations/{early.json()['record_id']}",
            headers=AUTH,
        )
        assert withdrawn.status_code == 200
        assert withdrawn.json() == {"deleted": True, "synthetic_only": True}

        denied = client.post(
            f"/ops/observations?marker={marker}",
            content=f'{{"note":"{marker}"}}',
            headers={"Authorization": f"Bearer wrong-{marker}"},
        )
        assert denied.status_code == 401
        assert marker not in denied.text

        invalid_cases = [
            observation(note=marker),
            observation(scenario="real"),
            observation(consent="real_participant_granted"),
            observation(task_id="unknown-task"),
        ]
        for payload in invalid_cases:
            rejected = client.post(
                "/ops/observations",
                json=payload,
                headers={**AUTH, "Idempotency-Key": str(uuid.uuid4())},
            )
            assert rejected.status_code == 422
            assert rejected.json() == {"error": "invalid_record"}

        too_large = client.post(
            "/ops/observations",
            content=b" " * 9000,
            headers={**AUTH, "Idempotency-Key": str(uuid.uuid4())},
        )
        assert too_large.status_code == 413
        assert too_large.json() == {"error": "invalid_request"}

        # The protected preparation surface remains preparation-only; legacy
        # participant/session routes are not silently enabled by storage.
        for path in ("/api/consult", "/api/session/example"):
            response = client.post(path, json={"note": marker}, headers=AUTH)
            assert response.status_code == 404

    serialized = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (private_root / "app-runtime-v1").rglob("*.json")
    )
    assert marker not in serialized
    assert TOKEN not in serialized
    assert not list(runtime.store.observations.glob("*.json"))
    assert not [r for r in caplog.records if r.name == "youth_law.preparation"]


def test_private_server_handler_discards_exception_text_and_keeps_category(private_root):
    runtime = make_runtime(private_root, Clock())
    with TestClient(create_preview_app(runtime)):
        secret = "PRIVATE_SERVER_TRACEBACK_SENTINEL_987654321"
        record = logging.LogRecord(
            "uvicorn.error",
            logging.ERROR,
            __file__,
            99,
            secret,
            (),
            None,
        )
        record.exc_info = (
            RuntimeError,
            RuntimeError(secret),
            None,
        )
        PrivateServerLogHandler(runtime).emit(record)

    stored = list(runtime.store.logs.glob("*.json"))
    assert len(stored) == 1
    serialized = stored[0].read_text(encoding="utf-8")
    assert secret not in serialized
    payload = json.loads(serialized)["payload"]
    assert payload["event"] == "server_diagnostic"
    assert payload["route"] == "other"
    assert payload["status"] == 500
    assert payload["exception_class"] == "RuntimeError"


def test_operator_observation_requires_canonical_idempotency_key(private_root):
    runtime = make_runtime(private_root, Clock())
    with TestClient(
        create_protected_app(TOKEN, private_runtime=runtime), client=("127.0.0.1", 50000)
    ) as client:
        for key in (None, "", "not-a-uuid", str(uuid.uuid4()).upper()):
            headers = dict(AUTH)
            if key is not None:
                headers["Idempotency-Key"] = key
            response = client.post("/ops/observations", json=observation(), headers=headers)
            assert response.status_code == 422
            assert response.json() == {"error": "invalid_record"}
        assert not list(runtime.store.observations.glob("*.json"))
