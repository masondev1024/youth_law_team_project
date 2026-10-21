"""Authentication and bounded browser-session regression contracts."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from test_rehearsal_app import ORIGIN, TOKEN, action, local_client, make_app

from app.rehearsal_sessions import MAX_SESSIONS, RehearsalError

pytest_plugins = ("test_rehearsal_app",)


COOKIE_NAME = "youth_law_rehearsal_8015"


@pytest.mark.parametrize("release", ["logout", "expiration"])
def test_session_capacity_recovers_after_logout_or_expiration(private_root, release):
    app = make_app(private_root)
    with local_client(app) as client:
        issued = []
        for _ in range(MAX_SESSIONS):
            response = client.post(
                "/rehearsal/api/login",
                json={"token": TOKEN},
                headers={"Origin": ORIGIN},
            )
            assert response.status_code == 200
            issued.append((client.cookies.get(COOKIE_NAME), response.json()["csrf_token"]))
            client.cookies.clear()

        assert len(app.state.rehearsal_sessions.sessions) == MAX_SESSIONS
        full = client.post(
            "/rehearsal/api/login",
            json={"token": TOKEN},
            headers={"Origin": ORIGIN},
        )
        assert full.status_code == 429
        assert full.json() == {"error": "busy"}

        session_id, csrf = issued[0]
        if release == "logout":
            client.cookies.set(COOKIE_NAME, session_id)
            logged_out = action(client, "/rehearsal/api/logout", {}, csrf)
            assert logged_out.status_code == 200
            assert logged_out.json() == {"logged_out": True}
        else:
            app.state.rehearsal_sessions.sessions[session_id].expires = 0

        client.cookies.clear()
        recovered = client.post(
            "/rehearsal/api/login",
            json={"token": TOKEN},
            headers={"Origin": ORIGIN},
        )
        assert recovered.status_code == 200
        assert len(app.state.rehearsal_sessions.sessions) == MAX_SESSIONS


def test_concurrent_invalid_logins_observe_one_atomic_rate_limit_window(private_root):
    sessions = make_app(private_root).state.rehearsal_sessions

    def attempt(_):
        try:
            sessions.login("synthetic-invalid-credential")
        except RehearsalError as exc:
            return exc.status, exc.code
        pytest.fail("invalid credentials unexpectedly created a session")

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(attempt, range(24)))

    assert outcomes.count((401, "unauthorized")) == 10
    assert outcomes.count((429, "login_rate_limited")) == 14
    assert sessions.sessions == {}
    assert len(sessions.failed_logins) == 10


def test_browser_cookie_from_previous_runtime_cannot_replay_after_restart(private_root):
    first_app = make_app(private_root)
    with local_client(first_app) as first_client:
        first_login = first_client.post(
            "/rehearsal/api/login",
            json={"token": TOKEN},
            headers={"Origin": ORIGIN},
        )
        assert first_login.status_code == 200
        old_cookie = first_client.cookies.get(COOKIE_NAME)
        old_csrf = first_login.json()["csrf_token"]

    restarted_app = make_app(private_root)
    with local_client(restarted_app) as restarted_client:
        restarted_client.cookies.set(COOKIE_NAME, old_cookie)
        replay = restarted_client.get("/rehearsal/api/state")
        assert replay.status_code == 401
        assert replay.json() == {"error": "unauthorized"}

        restarted_client.cookies.clear()
        fresh = restarted_client.post(
            "/rehearsal/api/login",
            json={"token": TOKEN},
            headers={"Origin": ORIGIN},
        )
        assert fresh.status_code == 200
        assert fresh.json()["csrf_token"] != old_csrf
