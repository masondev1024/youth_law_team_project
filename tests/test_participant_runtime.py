"""Protected HTTP contract tests for the local participant rehearsal runtime.

These tests exercise the API and lifecycle boundary. Low-level SQLite, ACL,
and validation details belong in the participant-store tests.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.participant_storage import ParticipantStudyStore
from app.protected_study import create_app as create_protected_app
from app.study_api import create_app as create_preview_app
from app.study_runtime import PrivateStudyRuntime
from app.study_storage import PrivateStudyStore

TOKEN = "participant-runtime-integration-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOTICE_VERSION = "finance-formative-notice-20261020-r2"
POLICY_VERSION = "finance-formative-policy-20261020-r2"
TASK_ID = "formative-ux-repayment-01"
NOTICE_FIXTURE_BYTES = b"participant notice fixture canonical bytes r2"
POLICY_FIXTURE_BYTES = b"participant policy fixture canonical bytes r2"
NOTICE_SHA256 = hashlib.sha256(NOTICE_FIXTURE_BYTES).hexdigest()
POLICY_SHA256 = hashlib.sha256(POLICY_FIXTURE_BYTES).hexdigest()


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def private_root(tmp_path):
    """Use the same restrictive, real filesystem guard as production tests."""
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
    return PrivateStudyRuntime(
        PrivateStudyStore(private_root, clock=clock),
        participant_store=ParticipantStudyStore(
            private_root,
            notice_sha256=NOTICE_SHA256,
            policy_sha256=POLICY_SHA256,
            clock=clock,
        ),
        interval_seconds=0.03,
    )


def consent(**updates):
    value = {
        "adult_confirmed": True,
        "participation_agreed": True,
        "observation_recording_agreed": True,
        "notice_version": NOTICE_VERSION,
        "policy_version": POLICY_VERSION,
    }
    value.update(updates)
    return value


def request_key(clock: Clock) -> str:
    return f"{int(clock().timestamp())}:{uuid.uuid4()}"


def create_payload(**consent_updates):
    return {"task_id": TASK_ID, "consent": consent(**consent_updates)}


def observation(**updates):
    value = {
        "task_status": "completed",
        "elapsed_seconds_monotonic": 42,
        "help_requested_during_task": "no",
        "content_help_during_task": "no",
        "technical_issue_during_task": "no",
        "stuck_event_count_during_task": 0,
        "next_action_comprehension_post_task": "observed_understood",
        "source_link_date_location_comprehension_post_task": "observed_understood",
        "preparation_list_meaning_comprehension_post_task": "observed_understood",
        "card_limits_comprehension_post_task": "observed_understood",
        "critical_misunderstanding_observed": "no_observed",
        "critical_misunderstanding_phase": "unknown",
        "sensitive_data_handling_status": "not_observed",
        "recording_status": "not_recorded",
    }
    value.update(updates)
    return value


def create_record(client: TestClient, clock: Clock, *, key: str | None = None):
    return client.post(
        "/ops/participant-records/consents",
        json=create_payload(),
        headers={**AUTH, "Idempotency-Key": key or request_key(clock)},
    )


def wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_participant_routes_are_opt_in_and_share_protected_loopback_boundary(private_root):
    clock = Clock()
    runtime = make_runtime(private_root, clock)
    protected = create_protected_app(TOKEN, private_runtime=runtime)
    preview = create_preview_app(runtime)
    create_path = "/ops/participant-records/consents"
    status_path = "/ops/participant-records/status"

    with TestClient(preview) as client:
        assert client.get(status_path).status_code == 404
        assert client.post(create_path, json=create_payload()).status_code == 404

    with TestClient(protected, client=("127.0.0.1", 50000)) as client:
        assert client.get(status_path).status_code == 401
        assert (
            client.post(
                create_path,
                json=create_payload(),
                headers={"Idempotency-Key": request_key(clock)},
            ).status_code
            == 401
        )
        status = client.get(status_path, headers=AUTH)
        assert status.status_code == 200
        assert status.json()["session_admission"] is False
        assert status.json()["record_origin"] == "developer_rehearsal"

    remote = TestClient(protected, client=("198.51.100.20", 40000))
    denied = remote.get(status_path, headers=AUTH)
    assert denied.status_code == 404
    assert denied.json() == {"error": "not_found"}


def test_status_exposes_only_admission_provenance_contract_versions_and_counts(private_root):
    clock = Clock()
    runtime = make_runtime(private_root, clock)
    with TestClient(
        create_protected_app(TOKEN, private_runtime=runtime), client=("127.0.0.1", 50000)
    ) as client:
        state = client.get("/ops/participant-records/status", headers=AUTH)

    assert state.status_code == 200
    payload = state.json()
    assert payload["session_admission"] is False
    assert payload["record_origin"] == "developer_rehearsal"
    assert payload["notice_version"] == NOTICE_VERSION
    assert payload["policy_version"] == POLICY_VERSION
    assert payload["counts"]["participant_records"] == 0
    assert not {"records", "record_ids", "observations", "consents"} & payload.keys()


@pytest.mark.parametrize(
    "refusal",
    [
        {"participation_agreed": False},
        {"observation_recording_agreed": False},
    ],
)
def test_refused_consent_creates_no_record_or_identifier(private_root, refusal):
    clock = Clock()
    runtime = make_runtime(private_root, clock)
    with TestClient(
        create_protected_app(TOKEN, private_runtime=runtime), client=("127.0.0.1", 50000)
    ) as client:
        response = client.post(
            "/ops/participant-records/consents",
            json=create_payload(**refusal),
            headers={**AUTH, "Idempotency-Key": request_key(clock)},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "declined", "record_created": False}
    assert runtime.participant_store.summary()["developer_rehearsal_records"] == 0
    assert runtime.participant_store.summary()["participant_records"] == 0


def test_granted_consent_stamps_origin_expiry_and_server_pinned_versions(private_root):
    clock = Clock()
    runtime = make_runtime(private_root, clock)
    with TestClient(
        create_protected_app(TOKEN, private_runtime=runtime), client=("127.0.0.1", 50000)
    ) as client:
        response = create_record(client, clock)

    assert response.status_code == 201
    payload = response.json()
    assert payload["record_id"]
    expires_at = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
    assert expires_at == clock() + timedelta(days=30)
    assert payload["version"] == 1
    assert payload["record_origin"] == "developer_rehearsal"
    assert payload["session_admission"] is False
    stored = runtime.participant_store.read_record(payload["record_id"])
    assert stored["consent"] == consent()
    assert stored["notice_sha256"] == NOTICE_SHA256
    assert stored["policy_sha256"] == POLICY_SHA256


@pytest.mark.parametrize(
    "payload",
    [
        {"task_id": TASK_ID, "consent": consent(adult_confirmed=1)},
        {"task_id": TASK_ID, "consent": consent(adult_confirmed=False)},
        {"task_id": TASK_ID, "consent": consent(notice_version="caller-version")},
        {"task_id": TASK_ID, "consent": consent(email="private@example.test")},
        {"task_id": "unknown-task", "consent": consent()},
        {"task_id": TASK_ID, "consent": consent(), "record_origin": "participant"},
        {"task_id": TASK_ID, "consent": consent(), "expires_at": "2099-01-01T00:00:00Z"},
        {"task_id": TASK_ID, "consent": consent(), "email": "private@example.test"},
    ],
)
def test_create_rejects_invalid_or_extra_fields_before_record_creation(private_root, payload):
    clock = Clock()
    runtime = make_runtime(private_root, clock)
    with TestClient(
        create_protected_app(TOKEN, private_runtime=runtime), client=("127.0.0.1", 50000)
    ) as client:
        response = client.post(
            "/ops/participant-records/consents",
            json=payload,
            headers={**AUTH, "Idempotency-Key": request_key(clock)},
        )

    assert response.status_code == 422
    assert response.json() == {"error": "invalid_record"}
    assert runtime.participant_store.summary()["developer_rehearsal_records"] == 0


def test_observation_requires_full_coded_snapshot_and_rejects_free_text(private_root):
    clock = Clock()
    runtime = make_runtime(private_root, clock)
    secret = "PRIVATE_OBSERVATION_SENTINEL_927415"
    with TestClient(
        create_protected_app(TOKEN, private_runtime=runtime), client=("127.0.0.1", 50000)
    ) as client:
        created = create_record(client, clock)
        record_id = created.json()["record_id"]
        path = f"/ops/participant-records/{record_id}/observation"
        accepted = client.put(
            path,
            json={"expected_version": 1, "observation": observation()},
            headers=AUTH,
        )
        rejected = client.put(
            path,
            json={
                "expected_version": accepted.json()["version"],
                "observation": observation(operator_note=secret),
            },
            headers=AUTH,
        )
        partial = client.put(
            path,
            json={
                "expected_version": accepted.json()["version"],
                "observation": {"task_status": "completed"},
            },
            headers=AUTH,
        )
        stored = runtime.participant_store.read_record(record_id)

    assert accepted.status_code == 200
    assert accepted.json()["version"] == 2
    assert accepted.json()["record_id"] == record_id
    assert accepted.json()["expires_at"] == created.json()["expires_at"]
    assert accepted.json()["record_origin"] == "developer_rehearsal"
    assert accepted.json()["session_admission"] is False
    assert stored["observation_provenance"] == "operator_reported_rehearsal_state"
    assert stored["elapsed_seconds_provenance"] == "operator_observed"
    assert rejected.status_code == 422
    assert rejected.json() == {"error": "invalid_record"}
    assert partial.status_code == 422


def test_observation_compare_and_swap_and_identical_stale_retry(private_root):
    clock = Clock()
    runtime = make_runtime(private_root, clock)
    with TestClient(
        create_protected_app(TOKEN, private_runtime=runtime), client=("127.0.0.1", 50000)
    ) as client:
        created = create_record(client, clock).json()
        path = f"/ops/participant-records/{created['record_id']}/observation"
        body = {"expected_version": 1, "observation": observation()}
        first = client.put(path, json=body, headers=AUTH)
        retry = client.put(path, json=body, headers=AUTH)
        stale_change = client.put(
            path,
            json={
                "expected_version": 1,
                "observation": observation(stuck_event_count_during_task=1),
            },
            headers=AUTH,
        )
        next_version = client.put(
            path,
            json={
                "expected_version": 2,
                "observation": observation(stuck_event_count_during_task=1),
            },
            headers=AUTH,
        )

    assert first.status_code == 200
    assert first.json()["version"] == 2
    assert retry.status_code == 200
    assert retry.json()["version"] == 2
    assert stale_change.status_code == 409
    assert stale_change.json() == {"error": "conflict"}
    assert next_version.status_code == 200
    assert next_version.json()["version"] == 3


def test_withdrawal_is_idempotent_and_request_key_cannot_resurrect_after_restart(private_root):
    clock = Clock()
    first_runtime = make_runtime(private_root, clock)
    key = request_key(clock)
    first_app = create_protected_app(TOKEN, private_runtime=first_runtime)
    with TestClient(first_app, client=("127.0.0.1", 50000)) as client:
        created = create_record(client, clock, key=key)
        record_id = created.json()["record_id"]
        path = f"/ops/participant-records/{record_id}"
        withdrawn = client.delete(path, headers=AUTH)
        repeated = client.delete(path, headers=AUTH)
        replay = client.post(
            "/ops/participant-records/consents",
            json=create_payload(),
            headers={**AUTH, "Idempotency-Key": key},
        )

    assert withdrawn.status_code == 200
    assert withdrawn.json() == {"deleted": True, "record_origin": "developer_rehearsal"}
    assert repeated.status_code == 200
    assert repeated.json() == {"deleted": False, "record_origin": "developer_rehearsal"}
    assert replay.status_code == 409

    second_runtime = make_runtime(private_root, clock)
    with TestClient(
        create_protected_app(TOKEN, private_runtime=second_runtime), client=("127.0.0.1", 50000)
    ) as client:
        after_restart = client.post(
            "/ops/participant-records/consents",
            json=create_payload(),
            headers={**AUTH, "Idempotency-Key": key},
        )
    assert after_restart.status_code == 409
    assert second_runtime.participant_store.read_record(record_id) is None
    assert second_runtime.participant_store.summary()["developer_rehearsal_records"] == 0


def test_periodic_expiry_removes_record_and_runtime_logs_only_safe_route_categories(
    private_root, caplog
):
    clock = Clock()
    runtime = make_runtime(private_root, clock)
    caplog.set_level(logging.INFO)
    with TestClient(
        create_protected_app(TOKEN, private_runtime=runtime), client=("127.0.0.1", 50000)
    ) as client:
        created = create_record(client, clock, key=request_key(clock))
        record_id = created.json()["record_id"]
        cleanup_before = runtime.cleanup_runs
        clock.value += timedelta(days=30, seconds=1)
        assert wait_for(
            lambda: (
                runtime.cleanup_runs > cleanup_before
                and runtime.participant_store.read_record(record_id) is None
            )
        )

    serialized_logs = "\n".join(record.getMessage() for record in caplog.records)
    for private_value in (TOKEN, record_id):
        assert private_value not in serialized_logs
    for path in (private_root / "app-runtime-v1").rglob("*.json"):
        content = path.read_text(encoding="utf-8")
        assert TOKEN not in content
        assert record_id not in content
    assert runtime.participant_store.summary()["developer_rehearsal_records"] == 0


def test_participant_cleanup_failure_is_503_then_periodic_retry_recovers(private_root, monkeypatch):
    clock = Clock()
    store = ParticipantStudyStore(
        private_root,
        notice_sha256=NOTICE_SHA256,
        policy_sha256=POLICY_SHA256,
        clock=clock,
    )
    original_purge = store.purge_expired
    attempts = 0

    def fail_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("private participant cleanup marker")
        return original_purge()

    monkeypatch.setattr(store, "purge_expired", fail_once)
    runtime = PrivateStudyRuntime(
        PrivateStudyStore(private_root, clock=clock),
        participant_store=store,
        interval_seconds=0.03,
    )
    with TestClient(
        create_protected_app(TOKEN, private_runtime=runtime), client=("127.0.0.1", 50000)
    ) as client:
        unavailable = client.get("/ops/participant-records/status", headers=AUTH)
        assert unavailable.status_code == 503
        assert unavailable.json()["error"] == "storage_unavailable"
        assert wait_for(lambda: runtime.healthy and attempts >= 2)
        recovered = client.get("/ops/participant-records/status", headers=AUTH)

    assert recovered.status_code == 200
    assert attempts < 10
