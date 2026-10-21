"""HTTP and workflow contracts for the isolated developer rehearsal app."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.participant_models import NOTICE_VERSION, POLICY_VERSION
from app.participant_storage import ParticipantStudyStore
from app.rehearsal_app import create_app
from app.study_runtime import PrivateStudyRuntime
from app.study_storage import PrivateStudyStore, StudyStorageError

ORIGIN = "http://localhost:8015"
TOKEN = "rehearsal-browser-test-token-never-expose-001"
NOTICE_TEXT = (
    "# Private rehearsal notice\n\n"
    f"Version: {NOTICE_VERSION}\n"
    "Exact bytes for this test.\n"
)
NOTICE_SHA256 = hashlib.sha256(NOTICE_TEXT.encode("utf-8")).hexdigest()
POLICY_SHA256 = hashlib.sha256(b"test-only immutable policy fixture").hexdigest()
TASK_ID = "formative-ux-repayment-01"


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


def make_runtime(private_root, clock=None):
    clock = clock or Clock()
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


def make_app(private_root, runtime=None, *, token=TOKEN):
    return create_app(
        token,
        private_runtime=runtime or make_runtime(private_root),
        allowed_origin=ORIGIN,
        notice_text=NOTICE_TEXT,
    )


def local_client(app):
    return TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000))


def login(client: TestClient, *, token=TOKEN):
    response = client.post(
        "/rehearsal/api/login", json={"token": token}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 200
    state = response.json()
    assert isinstance(state["csrf_token"], str) and state["csrf_token"]
    return state, response


def state(client: TestClient):
    response = client.get("/rehearsal/api/state")
    assert response.status_code == 200
    return response.json()


def action(client: TestClient, path: str, payload: dict, csrf: str, *, origin=ORIGIN,
           extra_headers: dict | None = None):
    headers = {"Origin": origin, "X-Rehearsal-CSRF": csrf}
    headers.update(extra_headers or {})
    return client.post(path, json=payload, headers=headers)


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


def grant_record(client: TestClient, current_state: dict):
    response = action(
        client,
        "/rehearsal/api/consent",
        {"task_id": TASK_ID, "consent": consent()},
        current_state["csrf_token"],
    )
    assert response.status_code == 200
    return response.json()


def test_login_state_cookie_health_and_outer_security_headers(private_root):
    app = make_app(private_root)
    with local_client(app) as client:
        unauthorized = client.get("/rehearsal/api/state")
        assert unauthorized.status_code == 401
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["mode"] == "developer_rehearsal"
        assert health.json()["session_admission"] is False
        assert health.json()["application_input_storage"] is False

        login_state, login_response = login(client)
        assert login_state["session_admission"] is False
        assert login_state["record_origin"] == "developer_rehearsal"
        assert login_state["notice"] == {
            "text": NOTICE_TEXT,
            "notice_version": NOTICE_VERSION,
            "policy_version": POLICY_VERSION,
        }
        assert len(login_state["tasks"]) == 3
        first_task = next(task for task in login_state["tasks"] if task["id"] == TASK_ID)
        assert first_task["title"] == "상환 부담"
        assert all(task["scenario"] == "" for task in login_state["tasks"])
        cookie = login_response.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=strict" in cookie
        assert "path=/" in cookie
        assert "csrf" not in cookie
        assert TOKEN not in login_response.text
        assert client.cookies

        page = client.get("/rehearsal")
        assert page.status_code == 200
        assert page.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert page.headers["cache-control"].startswith("no-store")
        assert "localStorage" not in page.text
        assert "sessionStorage" not in page.text
        assert TOKEN not in page.text
        script = client.get("/static/rehearsal.js")
        assert script.status_code == 200
        assert script.headers["x-content-type-options"] == "nosniff"
        assert script.headers["cache-control"].startswith("no-store")
        assert "localStorage" not in script.text
        assert "sessionStorage" not in script.text
        assert TOKEN not in script.text


@pytest.mark.parametrize(
    "declined",
    [
        {"participation_agreed": False},
        {"observation_recording_agreed": False},
        {
            "adult_confirmed": False,
            "participation_agreed": False,
            "observation_recording_agreed": False,
        },
    ],
)
def test_refusal_returns_declined_state_without_creating_row(private_root, declined):
    runtime = make_runtime(private_root)
    with local_client(make_app(private_root, runtime)) as client:
        current, _ = login(client)
        response = action(
            client,
            "/rehearsal/api/consent",
            {"task_id": TASK_ID, "consent": consent(**declined)},
            current["csrf_token"],
        )
        assert response.status_code == 200
        result = response.json()
        assert result["last_action"] == "declined"
        assert result["record"] is None
        assert runtime.participant_store.summary()["developer_rehearsal_records"] == 0


def test_consent_is_idempotent_and_start_reveals_task_with_explicit_timing(private_root):
    runtime = make_runtime(private_root)
    with local_client(make_app(private_root, runtime)) as client:
        current, _ = login(client)
        first = grant_record(client, current)
        assert all(task["scenario"] == "" for task in first["tasks"])
        record_id = first["record"]["record_id"]
        repeated = action(
            client,
            "/rehearsal/api/consent",
            {"task_id": TASK_ID, "consent": consent()},
            first["csrf_token"],
        )
        assert repeated.status_code == 200
        assert repeated.json()["record"]["record_id"] == record_id
        assert runtime.participant_store.summary()["developer_rehearsal_records"] == 1

        before_start = client.get("/rehearsal/card")
        assert before_start.status_code == 409
        assert before_start.json() == {"error": "task_not_started"}
        started = action(
            client, "/rehearsal/api/start", {}, repeated.json()["csrf_token"]
        )
        assert started.status_code == 200
        revealed = [task for task in started.json()["tasks"] if task["scenario"]]
        assert len(revealed) == 1
        assert revealed[0]["id"] == TASK_ID
        workflow = started.json()["workflow"]
        assert workflow["started"] is True
        assert workflow["timing_seconds"] is None
        assert workflow["card_displayed"] is False

        frame = client.get("/rehearsal/card")
        assert frame.status_code == 200
        assert frame.headers["x-frame-options"] == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in frame.headers["content-security-policy"]
        options = client.get("/api/preparation/options")
        assert options.status_code == 200
        assert options.json()["topics"]
        card = client.post(
            "/api/preparation/card",
            json={"issue": "repayment", "selected_facts": ["working", "not_overdue"]},
            headers={"Origin": ORIGIN},
        )
        assert card.status_code == 200

        timing = action(
            client,
            "/rehearsal/api/timing",
            {"elapsed_seconds": 42, "card_displayed": True},
            started.json()["csrf_token"],
        )
        assert timing.status_code == 200
        assert timing.json()["workflow"]["timing_seconds"] == 42
        assert timing.json()["workflow"]["card_displayed"] is True


def test_start_retry_preserves_checkpoint_and_restart_is_available_only_before_timing(
    private_root,
):
    with local_client(make_app(private_root)) as client:
        current, _ = login(client)
        granted = grant_record(client, current)
        started = action(client, "/rehearsal/api/start", {}, granted["csrf_token"])
        assert started.status_code == 200
        assert started.json()["workflow"]["timing_seconds"] is None

        repeated = action(client, "/rehearsal/api/start", {}, started.json()["csrf_token"])
        assert repeated.status_code == 200
        assert repeated.json()["workflow"]["started"] is True
        assert repeated.json()["workflow"]["timing_seconds"] is None

        restarted = action(
            client, "/rehearsal/api/restart", {}, repeated.json()["csrf_token"]
        )
        assert restarted.status_code == 200
        assert restarted.json()["workflow"] == {
            "started": True,
            "timing_seconds": None,
            "card_displayed": False,
        }
        stopped = action(
            client,
            "/rehearsal/api/timing",
            {"elapsed_seconds": 42, "card_displayed": True},
            restarted.json()["csrf_token"],
        )
        assert stopped.status_code == 200
        assert stopped.json()["workflow"]["timing_seconds"] == 42
        assert stopped.json()["workflow"]["card_displayed"] is True

        repeated_after_timing = action(
            client, "/rehearsal/api/start", {}, stopped.json()["csrf_token"]
        )
        assert repeated_after_timing.status_code == 200
        assert repeated_after_timing.json()["workflow"]["timing_seconds"] == 42
        assert repeated_after_timing.json()["workflow"]["card_displayed"] is True
        denied_restart = action(
            client,
            "/rehearsal/api/restart",
            {},
            repeated_after_timing.json()["csrf_token"],
        )
        assert denied_restart.status_code == 409
        assert denied_restart.json() == {"error": "conflict"}


def test_observation_snapshot_version_conflict_and_withdrawal_removes_binding(private_root):
    runtime = make_runtime(private_root)
    with local_client(make_app(private_root, runtime)) as client:
        current, _ = login(client)
        granted = grant_record(client, current)
        record_id = granted["record"]["record_id"]
        started = action(
            client, "/rehearsal/api/start", {}, granted["csrf_token"]
        )
        assert started.status_code == 200

        saved = action(
            client,
            "/rehearsal/api/observations",
            {"expected_version": 1, "observation": observation()},
            started.json()["csrf_token"],
        )
        assert saved.status_code == 200
        assert saved.json()["last_action"] == "observation_saved"
        assert saved.json()["record"]["version"] == 2
        stale = action(
            client,
            "/rehearsal/api/observations",
            {
                "expected_version": 1,
                "observation": observation(stuck_event_count_during_task=1),
            },
            saved.json()["csrf_token"],
        )
        assert stale.status_code == 409
        assert stale.json() == {"error": "conflict"}

        withdrawn = action(
            client, "/rehearsal/api/withdraw", {}, saved.json()["csrf_token"]
        )
        assert withdrawn.status_code == 200
        assert withdrawn.json()["last_action"] == "withdrawn"
        assert withdrawn.json()["record"] is None
        assert all(task["scenario"] == "" for task in withdrawn.json()["tasks"])
        repeated = action(
            client, "/rehearsal/api/withdraw", {}, withdrawn.json()["csrf_token"]
        )
        assert repeated.status_code == 200
        assert repeated.json()["record"] is None
        assert runtime.participant_store.read_record(record_id) is None
        assert runtime.participant_store.summary()["developer_rehearsal_records"] == 0


@pytest.mark.parametrize("first_access", ["state", "observations", "card"])
def test_withdrawal_blocks_other_session_reads_writes_and_restore(private_root, first_access):
    runtime = make_runtime(private_root)
    with local_client(make_app(private_root, runtime)) as client:
        current, _ = login(client)
        granted = grant_record(client, current)
        record_id = granted["record"]["record_id"]
        saved = action(
            client, "/rehearsal/api/observations",
            {"expected_version": 1, "observation": observation()}, granted["csrf_token"],
        )
        assert saved.status_code == 200
        first_cookies = dict(client.cookies)

        # Two independently authenticated browser sessions hold the same record.
        other, _ = login(client)
        restored = action(
            client, "/rehearsal/api/restore", {"record_id": record_id}, other["csrf_token"]
        )
        assert restored.status_code == 200
        assert restored.json()["record"]["observation"] == observation()
        assert [task["id"] for task in restored.json()["tasks"] if task["scenario"]] == [
            TASK_ID
        ]
        other_cookies = dict(client.cookies)

        client.cookies.clear()
        client.cookies.update(first_cookies)
        withdrawn = action(client, "/rehearsal/api/withdraw", {}, granted["csrf_token"])
        assert withdrawn.status_code == 200

        client.cookies.clear()
        client.cookies.update(other_cookies)
        if first_access == "state":
            assert state(client)["record"] is None
        elif first_access == "observations":
            replay = action(
                client, "/rehearsal/api/observations",
                {"expected_version": 2, "observation": observation()}, other["csrf_token"],
            )
            assert replay.status_code == 410
        else:
            assert client.get("/rehearsal/card").status_code == 410

        cleared = state(client)
        assert cleared["record"] is None
        assert cleared["available_records"] == []
        assert cleared["workflow"] == {
            "started": False, "timing_seconds": None, "card_displayed": False,
        }
        replay = action(
            client, "/rehearsal/api/observations",
            {"expected_version": 2, "observation": observation()}, other["csrf_token"],
        )
        assert replay.status_code == 409
        assert replay.json() == {"error": "consent_required"}
        assert client.get("/rehearsal/card").status_code == 409
        restore = action(
            client, "/rehearsal/api/restore", {"record_id": record_id}, other["csrf_token"]
        )
        assert restore.status_code == 410
        assert runtime.participant_store.read_record(record_id) is None
        assert runtime.participant_store.summary()["developer_rehearsal_records"] == 0
        assert runtime.participant_store.summary()["revocations"] == 1


def test_existing_record_can_be_restored_after_new_runtime_and_login(private_root):
    first_runtime = make_runtime(private_root)
    with local_client(make_app(private_root, first_runtime)) as first_client:
        current, _ = login(first_client)
        granted = grant_record(first_client, current)
        record_id = granted["record"]["record_id"]
        logged_out = action(
            first_client, "/rehearsal/api/logout", {}, granted["csrf_token"]
        )
        assert logged_out.status_code == 200
        assert logged_out.json() == {"logged_out": True}
        assert "max-age=0" in logged_out.headers["set-cookie"].lower()
        assert not first_client.cookies
        assert first_client.get("/rehearsal/api/state").status_code == 401

    restarted_runtime = make_runtime(private_root)
    with local_client(make_app(private_root, restarted_runtime)) as second_client:
        current, _ = login(second_client)
        available = current["available_records"]
        assert any(row["record_id"] == record_id for row in available)
        restored = action(
            second_client,
            "/rehearsal/api/restore",
            {"record_id": record_id},
            current["csrf_token"],
        )
        assert restored.status_code == 200
        assert restored.json()["record"]["record_id"] == record_id
        assert restored.json()["workflow"]["started"] is True
        assert restored.json()["workflow"]["timing_seconds"] is None
        assert [task["id"] for task in restored.json()["tasks"] if task["scenario"]] == [
            TASK_ID
        ]
        assert restarted_runtime.participant_store.summary()["developer_rehearsal_records"] == 1


def test_expired_record_cannot_be_started_or_read(private_root):
    clock = Clock()
    runtime = make_runtime(private_root, clock)
    with local_client(make_app(private_root, runtime)) as client:
        current, _ = login(client)
        granted = grant_record(client, current)
        clock.value += timedelta(days=31)

        start = action(
            client, "/rehearsal/api/start", {}, granted["csrf_token"]
        )
        assert start.status_code == 410
        assert start.json() == {"error": "expired"}
        # The first expired access clears the stale session binding, so the
        # next access correctly reports that renewed consent is required.
        card = client.get("/rehearsal/card")
        assert card.status_code == 409
        assert card.json() == {"error": "consent_required"}


def test_factory_rejects_notice_text_that_differs_from_store_digest(private_root):
    clock = Clock()
    runtime = PrivateStudyRuntime(
        PrivateStudyStore(private_root, clock=clock),
        participant_store=ParticipantStudyStore(
            private_root,
            notice_sha256=hashlib.sha256(b"different private notice bytes").hexdigest(),
            policy_sha256=POLICY_SHA256,
            clock=clock,
        ),
        interval_seconds=0.03,
    )

    with pytest.raises(ValueError, match="notice content does not match"):
        create_app(
            TOKEN,
            private_runtime=runtime,
            allowed_origin=ORIGIN,
            notice_text=NOTICE_TEXT,
        )


def test_login_origin_csrf_host_forwarding_and_validation_guards(private_root):
    runtime = make_runtime(private_root)
    with local_client(make_app(private_root, runtime)) as client:
        bad_login = client.post(
            "/rehearsal/api/login",
            json={"token": "wrong"},
            headers={"Origin": ORIGIN},
        )
        assert bad_login.status_code == 401
        current, _ = login(client)

        missing_csrf = client.post(
            "/rehearsal/api/start", json={}, headers={"Origin": ORIGIN}
        )
        assert missing_csrf.status_code == 403
        wrong_origin = action(
            client,
            "/rehearsal/api/start",
            {},
            current["csrf_token"],
            origin="https://attacker.example",
        )
        assert wrong_origin.status_code == 403
        wrong_host = action(
            client,
            "/rehearsal/api/start",
            {},
            current["csrf_token"],
            extra_headers={"Host": "attacker.example"},
        )
        assert wrong_host.status_code in {400, 403, 404}
        forwarded = action(
            client,
            "/rehearsal/api/start",
            {},
            current["csrf_token"],
            extra_headers={"X-Forwarded-For": "127.0.0.1"},
        )
        assert forwarded.status_code in {403, 404}

        invalid = action(
            client,
            "/rehearsal/api/consent",
            {
                "task_id": TASK_ID,
                "consent": consent(),
                "email": "fixture@example.invalid",
            },
            current["csrf_token"],
        )
        assert invalid.status_code == 422
        assert runtime.participant_store.summary()["developer_rehearsal_records"] == 0

    unauthenticated = local_client(make_app(private_root))
    with unauthenticated:
        assert unauthenticated.get("/rehearsal/api/state").status_code == 401
        assert unauthenticated.get("/rehearsal/card").status_code == 401


def test_login_rate_limit_blocks_valid_credential_after_ten_failures(private_root):
    app = make_app(private_root)
    with local_client(app) as client:
        for _ in range(10):
            failed = client.post(
                "/rehearsal/api/login",
                json={"token": "invalid-login-token"},
                headers={"Origin": ORIGIN},
            )
            assert failed.status_code == 401

        blocked = client.post(
            "/rehearsal/api/login", json={"token": TOKEN}, headers={"Origin": ORIGIN}
        )
        assert blocked.status_code == 429
        assert blocked.json() == {"error": "login_rate_limited"}


def test_expired_browser_session_requires_fresh_login_and_explicit_restore(private_root):
    runtime = make_runtime(private_root)
    app = make_app(private_root, runtime)
    with local_client(app) as client:
        current, _ = login(client)
        granted = grant_record(client, current)
        record_id = granted["record"]["record_id"]
        cookie_name = "youth_law_rehearsal_8015"
        session_id = client.cookies.get(cookie_name)
        assert session_id
        app.state.rehearsal_sessions.sessions[session_id].expires = 0

        expired = client.get("/rehearsal/api/state")
        assert expired.status_code == 401
        fresh, _ = login(client)
        assert any(row["record_id"] == record_id for row in fresh["available_records"])
        restored = action(
            client,
            "/rehearsal/api/restore",
            {"record_id": record_id},
            fresh["csrf_token"],
        )
        assert restored.status_code == 200
        assert restored.json()["record"]["record_id"] == record_id
        assert runtime.participant_store.summary()["developer_rehearsal_records"] == 1


def test_login_requires_origin_and_json_and_oversized_body_is_rejected(private_root):
    with local_client(make_app(private_root)) as client:
        no_origin = client.post("/rehearsal/api/login", json={"token": TOKEN})
        assert no_origin.status_code == 403
        wrong_type = client.post(
            "/rehearsal/api/login",
            content=json.dumps({"token": TOKEN}),
            headers={"Origin": ORIGIN, "Content-Type": "text/plain"},
        )
        assert wrong_type.status_code == 422

        current, _ = login(client)
        oversized = client.post(
            "/rehearsal/api/start",
            content=b"x" * 8193,
            headers={
                "Origin": ORIGIN,
                "Content-Type": "application/json",
                "X-Rehearsal-CSRF": current["csrf_token"],
            },
        )
        assert oversized.status_code == 413
        assert oversized.json() == {"error": "invalid_request"}


@pytest.mark.parametrize(
    "changes,expected_version",
    [
        ({"elapsed_seconds_monotonic": True}, 1),
        ({"stuck_event_count_during_task": 1.0}, 1),
        ({}, True),
    ],
)
def test_observation_rejects_boolean_or_float_integer_fields_without_mutation(
    private_root, changes, expected_version
):
    runtime = make_runtime(private_root)
    with local_client(make_app(private_root, runtime)) as client:
        current, _ = login(client)
        granted = grant_record(client, current)
        started = action(client, "/rehearsal/api/start", {}, granted["csrf_token"])
        response = action(
            client,
            "/rehearsal/api/observations",
            {"expected_version": expected_version, "observation": observation(**changes)},
            started.json()["csrf_token"],
        )

    assert response.status_code == 422
    assert response.json() == {"error": "invalid_request"}
    stored = runtime.participant_store.read_record(granted["record"]["record_id"])
    assert stored["version"] == 1
    assert stored["observation"] is None


def test_rehearsal_cookie_does_not_authorize_operator_routes_or_query_token(private_root):
    with local_client(make_app(private_root)) as client:
        login(client)
        cookie_names = {cookie.name for cookie in client.cookies.jar}
        assert cookie_names == {"youth_law_rehearsal_8015"}
        assert client.get("/ops/participant-records/status").status_code == 404
        assert client.get(
            "/ops/retention", headers={"Authorization": f"Bearer {TOKEN}"}
        ).status_code == 404

    unauthenticated = local_client(make_app(private_root))
    with unauthenticated:
        query_auth = unauthenticated.get(f"/rehearsal/api/state?token={TOKEN}")
        assert query_auth.status_code == 401
        assert TOKEN not in query_auth.text


def test_unexpected_exception_and_storage_failure_are_safe_and_content_free(
    private_root, monkeypatch, caplog
):
    runtime = make_runtime(private_root)
    app = make_app(private_root, runtime)
    caplog.set_level(logging.INFO)
    with local_client(app) as client:
        current, _ = login(client)
        granted = grant_record(client, current)
        record_id = granted["record"]["record_id"]
        csrf = granted["csrf_token"]
        sentinel = "PRIVATE_REHEARSAL_EXCEPTION_BODY_SENTINEL_483"

        def explode(_session, _action, payload):
            raise RuntimeError(f"{sentinel}:{payload.task_id}:{record_id}:{csrf}:{TOKEN}")

        monkeypatch.setattr(app.state.rehearsal_sessions, "apply", explode)
        failed = action(
            client,
            "/rehearsal/api/consent",
            {"task_id": sentinel, "consent": consent()},
            csrf,
        )
        assert failed.status_code == 500
        assert failed.json() == {"error": "internal_error"}

        def fail_list(_limit=20):
            raise StudyStorageError("filesystem_error")

        monkeypatch.setattr(runtime.participant_store, "list_records", fail_list)
        unavailable = client.get("/rehearsal/api/state")
        assert unavailable.status_code == 503

    prohibited = (sentinel, record_id, csrf, TOKEN)
    all_logs = "\n".join(record.getMessage() for record in caplog.records)
    all_logs += "\n" + "\n".join(
        path.read_text(encoding="utf-8")
        for path in (private_root / "app-runtime-v1").rglob("*.json")
    )
    for value in prohibited:
        assert value not in failed.text
        assert value not in unavailable.text
        assert value not in all_logs
