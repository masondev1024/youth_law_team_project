#!/usr/bin/env python3
"""Bounded loopback TCP rehearsal for auth, capacity, and private-store recovery.

The runner creates a disposable synthetic store, passes an already-bound socket
to one uvicorn child at a time, and writes a content-free JSON evidence report.
It does not load the release policy or enable participant admission.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SYNTHETIC_ENV = "YOUTH_LAW_SYNTHETIC_RUNTIME_QA"
SYNTHETIC_MARKER = "synthetic-runtime-qa-v1\n"
BODY_SENTINEL = "SYNTHETIC_BODY_SENTINEL_REHEARSAL_QA"
TOKEN_NAME = "qa-token.txt"
NOTICE_NAME = "qa-notice.txt"
POLICY_NAME = "qa-policy.json"
CHILD_LIMIT_SECONDS = 120
STARTUP_SECONDS = 10
OBSERVATION = {
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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_private(path: Path, content: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _fixture(root: Path) -> tuple[str, str, str]:
    os.chmod(root, 0o700)
    _write_private(root / ".rehearsal-qa-owner", SYNTHETIC_MARKER.encode())
    storage_marker = {
        "schema": "youth-law-private-storage.v1",
        "owner_uid": os.getuid(),
        "real_participant_processing_enabled": False,
    }
    _write_private(
        root / ".study-storage.json",
        json.dumps(storage_marker, sort_keys=True, separators=(",", ":")).encode(),
    )
    control = root / "control"
    control.mkdir(mode=0o700)
    os.chmod(control, 0o700)
    token = "synthetic-qa-token-" + os.urandom(32).hex()
    notice = "Synthetic rehearsal notice for local runtime QA.\n"
    policy = json.dumps({"synthetic_only": True, "real_participant_admission": False})
    _write_private(root / TOKEN_NAME, (token + "\n").encode())
    _write_private(control / NOTICE_NAME, notice.encode())
    _write_private(control / POLICY_NAME, policy.encode())
    return token, notice, policy


def _scenario_visibility(state: dict[str, Any], selected_task_id: str | None = None) -> bool:
    tasks = state.get("tasks")
    if not isinstance(tasks, list):
        return False
    visible = [task.get("id") for task in tasks if task.get("scenario")]
    if selected_task_id is None:
        return not visible
    return visible == [selected_task_id]


def _safe_child_environment(root: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(REPO),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        SYNTHETIC_ENV: "1",
        "YOUTH_LAW_SYNTHETIC_ROOT": str(root.resolve()),
    }


def _listen() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.set_inheritable(True)
    return listener


class Harness:
    def __init__(self, root: Path, listener: socket.socket, token: str):
        self.root = root
        self.listener = listener
        self.port = listener.getsockname()[1]
        self.origin = f"http://localhost:{self.port}"
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.token = token
        self.deadline = time.monotonic() + CHILD_LIMIT_SECONDS
        self.child: subprocess.Popen[str] | None = None
        self._rss_stop = threading.Event()
        self._rss_thread: threading.Thread | None = None
        self.rss_max_kb = 0
        self.rss_samples = 0
        self.rss_limit_exceeded = False
        self.cases: list[dict[str, Any]] = []
        self.failures: list[dict[str, str]] = []
        self.http_requests = 0
        self._request_count_lock = threading.Lock()
        self.peak_connections = 0
        self.csrf_values: set[str] = set()
        self.load_metrics: dict[str, dict[str, float | int]] = {}

    def record(self, name: str, expected: Any, actual: Any, elapsed: float) -> None:
        passed = expected == actual
        item = {
            "scenario": name,
            "expected": expected,
            "actual": actual,
            "status": "pass" if passed else "fail",
            "duration_ms": round(elapsed * 1000, 2),
        }
        self.cases.append(item)
        if not passed:
            self.failures.append({"scenario": name, "reason": "expectation_mismatch"})

    def request(self, method: str, path: str, *, client: Any = None, **kwargs: Any):
        with self._request_count_lock:
            self.http_requests += 1
        started = time.monotonic()
        if client is None:
            with self._client() as temporary_client:
                response = temporary_client.request(method, path, **kwargs)
        else:
            response = client.request(method, path, **kwargs)
        return response, time.monotonic() - started

    def _client(self):
        import httpx

        timeout = max(0.1, min(8.0, self.deadline - time.monotonic()))
        return httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Host": f"localhost:{self.port}"},
            trust_env=False,
        )

    def start(self) -> bool:
        if self.child is not None and self.child.poll() is None:
            raise RuntimeError("only one rehearsal child may run at a time")
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--serve-synthetic",
            str(self.root.resolve()),
            str(self.listener.fileno()),
        ]
        self.child = subprocess.Popen(
            command,
            cwd=REPO,
            env=_safe_child_environment(self.root),
            pass_fds=(self.listener.fileno(),),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self._rss_stop.clear()
        self._rss_thread = threading.Thread(target=self._sample_child_rss, daemon=True)
        self._rss_thread.start()
        deadline = min(self.deadline, time.monotonic() + STARTUP_SECONDS)
        while time.monotonic() < deadline:
            if self.child.poll() is not None:
                return False
            try:
                response, elapsed = self.request("GET", "/health", timeout=0.5)
                if response.status_code == 200:
                    self.record("server_start", 200, response.status_code, elapsed)
                    return True
            except Exception:
                time.sleep(0.08)
        self.failures.append({"scenario": "server_start", "reason": "startup_timeout"})
        return False

    def stop(self, *, force: bool = False) -> tuple[int | None, str]:
        child = self.child
        if child is None:
            return None, ""
        if child.poll() is None:
            os.kill(child.pid, signal.SIGKILL if force else signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.kill(child.pid, signal.SIGKILL)
                child.wait(timeout=3)
        stderr = child.stderr.read(4096) if child.stderr else ""
        code = child.returncode
        self._rss_stop.set()
        if self._rss_thread is not None:
            self._rss_thread.join(timeout=2)
            self._rss_thread = None
        self.child = None
        if self.rss_limit_exceeded and not any(
            failure["scenario"] == "child_rss_limit" for failure in self.failures
        ):
            self.failures.append({"scenario": "child_rss_limit", "reason": "rss_over_256_mib"})
        return code, stderr

    def _sample_child_rss(self) -> None:
        """Sample child RSS with ps; this is bounded monitoring, not a peak guarantee."""
        while not self._rss_stop.wait(0.25):
            child = self.child
            if child is None or child.poll() is not None:
                return
            try:
                result = subprocess.run(
                    ["ps", "-o", "rss=", "-p", str(child.pid)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=1,
                )
                rss_kb = int(result.stdout.strip()) if result.returncode == 0 else 0
            except (OSError, ValueError, subprocess.TimeoutExpired):
                rss_kb = 0
            if rss_kb:
                self.rss_samples += 1
                self.rss_max_kb = max(self.rss_max_kb, rss_kb)
                if rss_kb > 256 * 1024:
                    self.rss_limit_exceeded = True
                    try:
                        os.kill(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    return

    def _headers(self, csrf: str | None = None, origin: str | None = None) -> dict[str, str]:
        headers = {"Origin": origin or self.origin}
        if csrf is not None:
            headers["X-Rehearsal-CSRF"] = csrf
        return headers

    def _login(self, *, client: Any = None):
        response, elapsed = self.request(
            "POST",
            "/rehearsal/api/login",
            client=client,
            json={"token": self.token},
            headers=self._headers(),
        )
        if response.status_code == 200:
            csrf = response.json().get("csrf_token")
            if isinstance(csrf, str):
                self.csrf_values.add(csrf)
        return response, elapsed

    def _action(self, client: Any, path: str, payload: dict, csrf: str):
        return self.request(
            "POST",
            f"/rehearsal/api/{path}",
            client=client,
            json=payload,
            headers=self._headers(csrf),
        )

    def boundary_checks(self) -> None:
        with self._client() as client:
            response, elapsed = self.request("GET", "/health", client=client)
            health = response.json()
            safe_health = {
                "status": health.get("status"),
                "session_admission": health.get("session_admission"),
                "external_model_enabled": health.get("external_model_enabled"),
            }
            self.record(
                "health_admission_closed",
                {"status": "ready", "session_admission": False, "external_model_enabled": False},
                safe_health,
                elapsed,
            )
            response, elapsed = self.request("GET", "/rehearsal/api/state", client=client)
            self.record("missing_cookie", 401, response.status_code, elapsed)
            response, elapsed = self._login(client=client)
            self.record("cookie_login", 200, response.status_code, elapsed)
            login_state = response.json() if response.status_code == 200 else {}
            self.record(
                "task_scenarios_hidden_at_login",
                True,
                _scenario_visibility(login_state),
                elapsed,
            )
            response, elapsed = self.request("GET", "/rehearsal/api/state", client=client)
            self.record("cookie_auth", 200, response.status_code, elapsed)
            response, elapsed = self.request(
                "POST",
                "/rehearsal/api/logout",
                client=client,
                json={},
                headers=self._headers("bad-csrf"),
            )
            self.record("csrf_rejected", 403, response.status_code, elapsed)
            response, elapsed = self.request(
                "POST",
                "/rehearsal/api/login",
                client=client,
                json={"token": self.token},
                headers=self._headers(origin="http://localhost:1"),
            )
            self.record("origin_rejected", 403, response.status_code, elapsed)
            response, elapsed = self.request(
                "GET",
                "/health",
                client=client,
                headers={"Host": f"localhost:{self.port}", "X-Forwarded-For": "127.0.0.1"},
            )
            self.record("forwarded_header_rejected", 404, response.status_code, elapsed)
            response, elapsed = self.request(
                "GET", "/health", client=client, headers={"Host": "wrong.invalid"}
            )
            self.record("wrong_host_rejected", 404, response.status_code, elapsed)
            response, elapsed = self.request(
                "POST",
                "/rehearsal/api/login",
                client=client,
                content=b"{",
                headers={"Origin": self.origin, "Content-Type": "application/json"},
            )
            self.record("malformed_json_rejected", 422, response.status_code, elapsed)
            response, elapsed = self.request(
                "POST",
                "/rehearsal/api/login",
                client=client,
                content=(BODY_SENTINEL.encode() + b" " * (8193 - len(BODY_SENTINEL))),
                headers={"Origin": self.origin, "Content-Type": "application/json"},
            )
            self.record("oversize_body_rejected", 413, response.status_code, elapsed)

    def slow_body_check(self) -> None:
        started = time.monotonic()
        status = _partial_body_status(self.port, 5.0)
        elapsed = time.monotonic() - started
        self.record(
            "slow_body_timeout",
            {"status": 408, "under_8_seconds": True},
            {"status": status, "under_8_seconds": elapsed < 8},
            elapsed,
        )
        response, elapsed = self.request("GET", "/health")
        self.record("health_after_slow_body", 200, response.status_code, elapsed)

    def capacity_check(self) -> None:
        sockets: list[socket.socket] = []
        started = time.monotonic()
        try:
            for _ in range(8):
                sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
                sock.settimeout(3)
                sock.sendall(
                    f"POST /rehearsal/api/login HTTP/1.1\r\nHost: localhost:{self.port}\r\n"
                    f"Origin: {self.origin}\r\nContent-Type: application/json\r\n"
                    'Content-Length: 100\r\nConnection: close\r\n\r\n{"token":'.encode()
                )
                sockets.append(sock)
            self.peak_connections = max(self.peak_connections, len(sockets) + 1)
            time.sleep(0.25)
            response = None
            elapsed = 0.0
            attempts = 0
            for _ in range(10):
                response, elapsed = self.request("GET", "/health")
                attempts += 1
                if response.status_code == 429:
                    break
                time.sleep(0.05)
            actual = {
                "status": response.status_code if response is not None else None,
                "retry_after": response.headers.get("Retry-After") if response else None,
                "attempts_under_10": attempts <= 10,
            }
            self.record(
                "capacity_rejected_ninth",
                {"status": 429, "retry_after": "1", "attempts_under_10": True},
                actual,
                elapsed,
            )
        except Exception:
            self.failures.append({"scenario": "capacity_rejected_ninth", "reason": "socket_error"})
        finally:
            for sock in sockets:
                try:
                    sock.close()
                except OSError:
                    pass
        elapsed = time.monotonic() - started
        if elapsed > 8:
            self.failures.append({"scenario": "capacity_recovery", "reason": "release_timeout"})
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            try:
                response, request_elapsed = self.request("GET", "/health")
                if response.status_code == 200:
                    self.record("capacity_recovered", 200, response.status_code, request_elapsed)
                    return
            except Exception:
                time.sleep(0.1)
        self.failures.append({"scenario": "capacity_recovered", "reason": "health_timeout"})

    def authenticated_load_checks(self, client: Any, csrf: str) -> None:
        """Exercise the actual authenticated card path with bounded concurrency."""
        from concurrent.futures import ThreadPoolExecutor

        cookie_name = f"youth_law_rehearsal_{self.port}"
        session_cookie = client.cookies.get(cookie_name)
        if not session_cookie:
            self.failures.append({"scenario": "load_session", "reason": "cookie_missing"})
            return

        payload = {"issue": "repayment", "selected_facts": ["working", "not_overdue"]}

        def card_request() -> tuple[int, dict[str, Any] | None, dict[str, Any] | None, float]:
            with self._client() as client:
                client.cookies.set(cookie_name, session_cookie)
                response, elapsed = self.request(
                    "POST",
                    "/api/preparation/card",
                    client=client,
                    json=payload,
                    headers=self._headers(csrf),
                )
                if response.status_code != 200:
                    return response.status_code, None, None, elapsed
                card = response.json()
                summary = {
                    "status": card.get("status"),
                    "issue": card.get("issue"),
                    "selected_fact_ids": [
                        fact.get("id") for fact in card.get("selected_facts", [])
                    ],
                }
                return response.status_code, card, summary, elapsed

        for concurrency in (1, 4, 8):
            before = self.http_requests
            with ThreadPoolExecutor(max_workers=concurrency) as executor:
                results = list(executor.map(lambda _: card_request(), range(concurrency)))
            statuses = [status for status, _, _, _ in results]
            payloads = [card for _, card, _, _ in results]
            summaries = [summary for _, _, summary, _ in results]
            durations = sorted(elapsed * 1000 for _, _, _, elapsed in results)
            consistent = bool(payloads) and all(payload == payloads[0] for payload in payloads)
            payload_matches = bool(summaries) and all(
                summary
                == {
                    "status": "ready",
                    "issue": "repayment",
                    "selected_fact_ids": ["working", "not_overdue"],
                }
                for summary in summaries
            )
            request_count = self.http_requests - before
            expected = {
                "request_count": concurrency,
                "all_status_200": True,
                "payload_consistent": True,
                "payload_matches_contract": True,
                "requests_at_most_36": True,
            }
            actual = {
                "request_count": request_count,
                "all_status_200": all(status == 200 for status in statuses),
                "payload_consistent": consistent,
                "payload_matches_contract": payload_matches,
                "requests_at_most_36": request_count <= 36,
            }
            self.record(
                f"authenticated_load_{concurrency}",
                expected,
                actual,
                max((elapsed for _, _, _, elapsed in results), default=0.0),
            )
            if self.cases[-1]["status"] != "pass":
                return
            self.load_metrics[str(concurrency)] = {
                "requests": request_count,
                "p50_ms": round(_percentile(durations, 0.50), 2),
                "p95_ms": round(_percentile(durations, 0.95), 2),
            }
            self.peak_connections = max(self.peak_connections, concurrency)

    def consent_and_observation(self) -> tuple[Any, str, dict]:
        client = self._client()
        response, elapsed = self._login(client=client)
        self.record("recovery_login", 200, response.status_code, elapsed)
        csrf = response.json()["csrf_token"]
        consent = {
            "adult_confirmed": True,
            "participation_agreed": True,
            "observation_recording_agreed": True,
            "notice_version": "finance-formative-notice-20261020-r2",
            "policy_version": "finance-formative-policy-20261020-r2",
        }
        response, elapsed = self._action(
            client, "consent", {"task_id": "formative-ux-repayment-01", "consent": consent}, csrf
        )
        self.record("synthetic_consent", 200, response.status_code, elapsed)
        consent_state = response.json() if response.status_code == 200 else {}
        self.record(
            "task_scenarios_hidden_after_consent",
            True,
            _scenario_visibility(consent_state),
            elapsed,
        )
        unsaved_record_id = consent_state.get("record", {}).get("record_id", "")
        response, elapsed = self._action(client, "restore", {"record_id": unsaved_record_id}, csrf)
        self.record("unsaved_record_restore", 200, response.status_code, elapsed)
        self.record(
            "selected_task_scenario_after_unsaved_restore",
            True,
            _scenario_visibility(
                response.json() if response.status_code == 200 else {},
                "formative-ux-repayment-01",
            ),
            elapsed,
        )
        response, elapsed = self._action(client, "start", {}, csrf)
        self.record("task_start", 200, response.status_code, elapsed)
        started_state = response.json() if response.status_code == 200 else {}
        self.record(
            "only_selected_task_scenario_after_start",
            True,
            _scenario_visibility(started_state, "formative-ux-repayment-01"),
            elapsed,
        )
        self.authenticated_load_checks(client, csrf)
        if any(
            case["scenario"].startswith("authenticated_load_") and case["status"] != "pass"
            for case in self.cases
        ):
            client.close()
            raise RuntimeError("authenticated_card_load_failed")
        response, elapsed = self._action(
            client,
            "observations",
            {"expected_version": 1, "observation": OBSERVATION},
            csrf,
        )
        self.record("synthetic_observation", 200, response.status_code, elapsed)
        return client, csrf, response.json()

    def recovery_check(self) -> tuple[Any, str, str]:
        client, _, state = self.consent_and_observation()
        record = state.get("record") or {}
        record_id = record.get("record_id", "")
        cookie_name = f"youth_law_rehearsal_{self.port}"
        stale_cookie = client.cookies.get(cookie_name)
        client.close()
        code, _ = self.stop(force=True)
        if code != -signal.SIGKILL:
            self.failures.append({"scenario": "crash_restart", "reason": "child_not_killed"})
        if not self.start():
            self.failures.append({"scenario": "crash_restart", "reason": "restart_failed"})
            return None, "", record_id
        client = self._client()
        if stale_cookie:
            client.cookies.set(cookie_name, stale_cookie)
        response, elapsed = self.request("GET", "/rehearsal/api/state", client=client)
        self.record("stale_cookie_rejected", 401, response.status_code, elapsed)
        client.cookies.clear()
        response, elapsed = self._login(client=client)
        self.record("fresh_login_after_restart", 200, response.status_code, elapsed)
        csrf = response.json().get("csrf_token", "")
        if csrf:
            self.csrf_values.add(csrf)
        response, elapsed = self._action(client, "restore", {"record_id": record_id}, csrf)
        self.record("record_restore_after_restart", 200, response.status_code, elapsed)
        self.record(
            "selected_task_scenario_after_saved_observation_restore",
            True,
            _scenario_visibility(
                response.json() if response.status_code == 200 else {},
                "formative-ux-repayment-01",
            ),
            elapsed,
        )
        restored_record = response.json().get("record") if response.status_code == 200 else None
        exact = (
            isinstance(restored_record, dict)
            and restored_record.get("record_id") == record_id
            and restored_record.get("observation") == OBSERVATION
            and restored_record.get("version") == 2
        )
        self.record("exact_observation_recovery", True, exact, elapsed)
        return client, csrf, record_id

    def withdrawal_and_corruption_check(self, client: Any, csrf: str, record_id: str) -> None:
        if client is None or not record_id:
            self.failures.append({"scenario": "withdrawal", "reason": "recovery_unavailable"})
            return
        database = self.root / "participant-runtime-v1" / "participant.sqlite3"
        old_backup = self.root / "pre-withdrawal.backup"
        _write_private(old_backup, b"")
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(old_backup) as target:
                source.backup(target)
        old_backup_hash = _sha256(old_backup)
        integrity, before_counts = _readonly_store_audit(old_backup)
        self.record(
            "pre_withdrawal_backup_contains_record",
            {"integrity": "ok", "records": 1, "revocations": 0},
            {"integrity": integrity, **before_counts},
            0.0,
        )
        response, elapsed = self._action(client, "withdraw", {}, csrf)
        self.record("withdrawal", 200, response.status_code, elapsed)
        if response.status_code != 200:
            client.close()
            return
        self.record(
            "task_scenarios_cleared_after_withdrawal",
            True,
            _scenario_visibility(response.json()),
            elapsed,
        )
        client.close()
        self.stop()

        self.restore_prewithdrawal_backup(old_backup, old_backup_hash)
        if self.failures:
            return
        backup = self.root / "verified-closed-store.backup"
        backup_sha = _sha256(database)
        shutil.copyfile(database, backup)
        os.chmod(backup, 0o600)
        backup_ok = _sha256(backup) == backup_sha
        integrity, counts = _readonly_store_audit(database)
        self.record(
            "withdrawal_ledger_integrity",
            {"integrity": "ok", "records": 0, "revocations": 1},
            {"integrity": integrity, **counts},
            0.0,
        )
        if integrity != "ok" or counts != {"records": 0, "revocations": 1}:
            return
        self.audit_private_logs(record_id)
        if self.failures:
            return

        corrupted = self.root / "corrupt-copy.sqlite3"
        shutil.copyfile(backup, corrupted)
        os.chmod(corrupted, 0o600)
        with corrupted.open("r+b") as stream:
            stream.truncate(100)
        # Exercise actual startup validation against a corrupted database, then
        # restore the verified bytes into the exact managed path.
        shutil.copyfile(corrupted, database)
        self.stop()
        started = self.start()
        child_code, child_stderr = (None, "")
        if not started:
            child_code, child_stderr = self.stop()
        safe_failure = (
            not started
            and child_code == 2
            and "private study startup failed" in child_stderr
            and not any(secret in child_stderr for secret in (self.token, record_id))
        )
        self.record("corrupt_store_fails_closed", True, safe_failure, 0.0)
        shutil.copyfile(backup, database)
        os.chmod(database, 0o600)
        self.record(
            "backup_digest_verified", True, backup_ok and _sha256(database) == backup_sha, 0.0
        )
        if not self.start():
            self.failures.append(
                {"scenario": "verified_backup_restart", "reason": "restart_failed"}
            )
            return
        with self._client() as restored:
            response, elapsed = self._login(client=restored)
            self.record("login_after_backup_restore", 200, response.status_code, elapsed)
            csrf_after = (
                response.json().get("csrf_token", "") if response.status_code == 200 else ""
            )
            response, elapsed = self.request("GET", "/rehearsal/api/state", client=restored)
            available = (
                response.json().get("available_records", []) if response.status_code == 200 else []
            )
            self.record("withdrawn_state_not_resurrected", 0, len(available), elapsed)
            response, elapsed = self._action(
                restored, "restore", {"record_id": record_id}, csrf_after
            )
            self.record("withdrawn_record_restore_denied", 410, response.status_code, elapsed)

    def restore_prewithdrawal_backup(self, backup: Path, source_hash: str) -> None:
        """Use the actual offline recovery API before restarting the HTTP child."""
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        from app.participant_recovery import restore_database
        from app.participant_storage import ParticipantStudyStore

        store = ParticipantStudyStore(
            self.root,
            notice_sha256=_sha256(self.root / "control" / NOTICE_NAME),
            policy_sha256=_sha256(self.root / "control" / POLICY_NAME),
        )
        started = time.monotonic()
        result = restore_database(
            store, backup, expected_ledger_checkpoint=store.current_ledger_checkpoint()
        )
        self.record(
            "old_backup_latest_deletion_applied",
            1,
            result["records_withdrawn"],
            time.monotonic() - started,
        )
        self.record("old_backup_source_preserved", True, _sha256(backup) == source_hash, 0.0)
        integrity, counts = _readonly_store_audit(store.database)
        self.record(
            "old_backup_installed_without_withdrawn_record",
            {"integrity": "ok", "records": 0, "revocations": 1},
            {"integrity": integrity, **counts},
            0.0,
        )

    def audit_private_logs(self, record_id: str) -> None:
        repository_path = str(REPO.resolve())
        if repository_path not in sys.path:
            sys.path.insert(0, repository_path)
        from app.study_storage import PrivateStudyStore

        logs_directory = self.root / "app-runtime-v1" / "logs"
        log_files = list(logs_directory.glob("*.json"))
        forbidden = [self.token, record_id, BODY_SENTINEL, *self.csrf_values]
        allowlist_valid = True
        forbidden_found = False
        for path in log_files:
            raw = path.read_bytes()
            if any(value and value.encode() in raw for value in forbidden):
                forbidden_found = True
            try:
                envelope = json.loads(raw)
                PrivateStudyStore._validate_log_payload(envelope["payload"])
            except Exception:
                allowlist_valid = False
        self.record(
            "private_log_allowlist_and_redaction",
            {"allowlist_valid": True, "forbidden_values_absent": True, "log_count_positive": True},
            {
                "allowlist_valid": allowlist_valid,
                "forbidden_values_absent": not forbidden_found,
                "log_count_positive": len(log_files) > 0,
            },
            0.0,
        )

    def shutdown(self) -> None:
        if self.child is not None:
            self.stop()


def _partial_body_status(port: int, body_wait: float) -> int | None:
    sock = socket.create_connection(("127.0.0.1", port), timeout=3)
    try:
        sock.settimeout(7.5)
        sock.sendall(
            f"POST /rehearsal/api/login HTTP/1.1\r\nHost: localhost:{port}\r\n"
            f"Origin: http://localhost:{port}\r\nContent-Type: application/json\r\n"
            "Content-Length: 100\r\nConnection: close\r\n\r\n{".encode()
        )
        time.sleep(body_wait)
        data = sock.recv(1024)
        line = data.split(b"\r\n", 1)[0]
        try:
            return int(line.split()[1])
        except (IndexError, ValueError):
            return None
    finally:
        sock.close()


def _readonly_store_audit(path: Path) -> tuple[str, dict[str, int]]:
    uri = f"file:{path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        records = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        revocations = connection.execute("SELECT COUNT(*) FROM revocations").fetchone()[0]
        return integrity, {"records": records, "revocations": revocations}


def _serve_synthetic(root: Path, inherited_fd: int) -> int:
    # This hidden entry point can only run with an explicit synthetic flag,
    # exact generated root, and its fixture marker. No credentials are argv.
    expected_root = os.environ.get("YOUTH_LAW_SYNTHETIC_ROOT")
    if (
        os.environ.get(SYNTHETIC_ENV) != "1"
        or not expected_root
        or root.resolve() != Path(expected_root).resolve()
        or not root.name.startswith("youth-law-runtime-qa-")
        or root.stat().st_mode & 0o777 != 0o700
        or (root / ".rehearsal-qa-owner").read_text(encoding="ascii") != SYNTHETIC_MARKER
    ):
        return 3
    try:
        from app.participant_storage import ParticipantStudyStore
        from app.rehearsal_app import create_app
        from app.study_runtime import PrivateStudyRuntime, private_server_log_config
        from app.study_storage import PrivateStudyStore

        token_bytes = (root / TOKEN_NAME).read_bytes()
        if len(token_bytes) > 128 or not token_bytes.endswith(b"\n"):
            return 3
        token = token_bytes[:-1].decode("ascii")
        notice = (root / "control" / NOTICE_NAME).read_text(encoding="utf-8")
        policy = (root / "control" / POLICY_NAME).read_bytes()
        if b'"real_participant_admission": false' not in policy:
            return 3
        notice_sha = hashlib.sha256(notice.encode()).hexdigest()
        policy_sha = hashlib.sha256(policy).hexdigest()
        participant_store = ParticipantStudyStore(
            root, notice_sha256=notice_sha, policy_sha256=policy_sha
        )
        runtime = PrivateStudyRuntime(PrivateStudyStore(root), participant_store=participant_store)
        sock = socket.socket(fileno=os.dup(inherited_fd))
        port = sock.getsockname()[1]
        app = create_app(
            token,
            private_runtime=runtime,
            allowed_origin=f"http://localhost:{port}",
            notice_text=notice,
        )
        import uvicorn

        server = uvicorn.Server(
            uvicorn.Config(
                app,
                log_config=private_server_log_config(runtime),
                access_log=False,
                proxy_headers=False,
                forwarded_allow_ips="",
                workers=1,
            )
        )
        server.run(sockets=[sock])
        return 0
    except Exception:
        print("private study startup failed", file=sys.stderr)
        return 2


def run(report_path: Path) -> int:
    started = time.monotonic()
    report_path = report_path.resolve()
    if report_path.exists():
        raise FileExistsError("report path already exists")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {}
    internal_failure_frames: list[dict[str, Any]] = []
    temp_root: Path | None = None
    cleanup: dict[str, bool] = {}
    with tempfile.TemporaryDirectory(prefix="youth-law-runtime-qa-") as temp:
        root = Path(temp).resolve()
        temp_root = root
        token, _, _ = _fixture(root)
        listener = _listen()
        harness = Harness(root, listener, token)
        previous_alarm_handler = signal.getsignal(signal.SIGALRM)

        def timeout_handler(_signum: int, _frame: Any) -> None:
            raise TimeoutError("overall rehearsal QA timeout")

        signal.signal(signal.SIGALRM, timeout_handler)
        signal.setitimer(signal.ITIMER_REAL, CHILD_LIMIT_SECONDS)
        try:
            if not harness.start():
                code, stderr = harness.stop()
                harness.failures.append(
                    {
                        "scenario": "server_start",
                        "reason": "child_exit" if code is not None else "no_child",
                    }
                )
                if stderr and "private study startup failed" not in stderr:
                    harness.failures.append({"scenario": "server_start", "reason": "unsafe_stderr"})
            else:
                harness.boundary_checks()
                if _phase_status(harness.cases, _PHASES["authentication_and_boundary"]) == "pass":
                    harness.slow_body_check()
                    harness.capacity_check()
                    if _phase_status(harness.cases, _PHASES["slow_body_and_capacity"]) == "pass":
                        client, csrf, record_id = harness.recovery_check()
                        if (
                            _phase_status(harness.cases, _PHASES["authenticated_load"]) == "pass"
                            and _phase_status(harness.cases, _PHASES["crash_recovery"]) == "pass"
                        ):
                            harness.withdrawal_and_corruption_check(client, csrf, record_id)
        except Exception as exc:
            # Keep exception detail out of evidence because it may contain payloads.
            harness.failures.append({"scenario": "harness", "reason": type(exc).__name__})
            for frame in traceback.extract_tb(exc.__traceback__):
                try:
                    source = Path(frame.filename).resolve().relative_to(REPO).as_posix()
                except ValueError:
                    continue
                internal_failure_frames.append(
                    {"file": source, "function": frame.name, "line": frame.lineno}
                )
        finally:
            try:
                harness.shutdown()
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous_alarm_handler)
                listener.close()
        phases = {name: _phase_status(harness.cases, names) for name, names in _PHASES.items()}
        cleanup = {
            "child_reaped": harness.child is None,
            "listener_closed": listener.fileno() == -1,
        }
        if harness.rss_samples == 0:
            harness.failures.append(
                {"scenario": "child_memory_observation", "reason": "no_rss_samples"}
            )
        if harness.peak_connections > 12:
            harness.failures.append(
                {"scenario": "connection_bound", "reason": "over_12_connections"}
            )
        report = {
            "schema": "rehearsal-runtime-qa.v1",
            "result": "pending_cleanup",
            "synthetic_only": True,
            "participant_admission": False,
            "external_models": False,
            "overall_timeout_seconds": CHILD_LIMIT_SECONDS,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "counts": {
                "scenarios": len(harness.cases),
                "http_requests": harness.http_requests,
                "peak_connections": harness.peak_connections,
            },
            "child_memory_observation": {
                "limit_bytes": 256 * 1024 * 1024,
                "sampling_method": "ps RSS sampled every 250ms",
                "scope": "sampled observations, not a peak-memory guarantee",
                "samples": harness.rss_samples,
                "max_sampled_rss_kb": harness.rss_max_kb,
                "limit_exceeded": harness.rss_limit_exceeded,
            },
            "load_metrics": harness.load_metrics,
            "load_endpoint": "/api/preparation/card",
            "phases": phases,
            "cleanup": cleanup,
            "cases": harness.cases,
            "failures": harness.failures,
            "internal_failure_frames": internal_failure_frames,
            "source_sha256": {
                name: _sha256(REPO / name)
                for name in [
                    "scripts/check_rehearsal_runtime.py",
                    "app/rehearsal_app.py",
                    "app/rehearsal_sessions.py",
                    "app/participant_storage.py",
                    "app/study_storage.py",
                    "app/study_runtime.py",
                    "app/participant_deletion_ledger.py",
                    "app/participant_recovery.py",
                ]
            },
        }

    cleanup["temporary_root_removed"] = temp_root is not None and not temp_root.exists()
    cleanup_pass = all(cleanup.values())
    if not cleanup_pass:
        report["failures"].append({"scenario": "cleanup", "reason": "cleanup_incomplete"})
    report["cleanup"] = cleanup
    report["result"] = (
        "pass"
        if _overall_pass(report["phases"], report["failures"], report["cases"])
        and cleanup_pass
        and not report["child_memory_observation"]["limit_exceeded"]
        else "fail"
    )
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    _write_report(report_path, report)
    return 0 if report["result"] == "pass" else 1


_PHASES: dict[str, tuple[str, ...]] = {
    "authentication_and_boundary": (
        "health_admission_closed",
        "missing_cookie",
        "cookie_login",
        "task_scenarios_hidden_at_login",
        "cookie_auth",
        "csrf_rejected",
        "origin_rejected",
        "forwarded_header_rejected",
        "wrong_host_rejected",
        "malformed_json_rejected",
        "oversize_body_rejected",
    ),
    "slow_body_and_capacity": (
        "slow_body_timeout",
        "health_after_slow_body",
        "capacity_rejected_ninth",
        "capacity_recovered",
    ),
    "authenticated_load": (
        "authenticated_load_1",
        "authenticated_load_4",
        "authenticated_load_8",
    ),
    "crash_recovery": (
        "recovery_login",
        "synthetic_consent",
        "task_scenarios_hidden_after_consent",
        "unsaved_record_restore",
        "selected_task_scenario_after_unsaved_restore",
        "task_start",
        "only_selected_task_scenario_after_start",
        "synthetic_observation",
        "stale_cookie_rejected",
        "fresh_login_after_restart",
        "record_restore_after_restart",
        "selected_task_scenario_after_saved_observation_restore",
        "exact_observation_recovery",
    ),
    "withdrawal_and_log_privacy": (
        "withdrawal",
        "task_scenarios_cleared_after_withdrawal",
        "withdrawal_ledger_integrity",
        "private_log_allowlist_and_redaction",
        "withdrawn_state_not_resurrected",
        "withdrawn_record_restore_denied",
    ),
    "pre_withdrawal_backup_recovery": (
        "pre_withdrawal_backup_contains_record",
        "old_backup_latest_deletion_applied",
        "old_backup_source_preserved",
        "old_backup_installed_without_withdrawn_record",
    ),
    "corrupt_store_fail_closed": (
        "corrupt_store_fails_closed",
        "backup_digest_verified",
        "login_after_backup_restore",
    ),
}


def _phase_status(cases: list[dict[str, Any]], names: tuple[str, ...]) -> str:
    found = [case for case in cases if case["scenario"] in names]
    return (
        "pass" if len(found) == len(names) and all(c["status"] == "pass" for c in found) else "fail"
    )


def _percentile(sorted_values: list[float], quantile: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, round((len(sorted_values) - 1) * quantile)))
    return sorted_values[index]


def _overall_pass(
    phases: dict[str, str], failures: list[dict[str, str]], cases: list[dict[str, Any]]
) -> bool:
    return (
        set(phases) == set(_PHASES)
        and all(status == "pass" for status in phases.values())
        and not failures
        and all(case["status"] == "pass" for case in cases)
    )


def _write_report(path: Path, report: dict[str, Any]) -> None:
    data = json.dumps(report, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--serve-synthetic", nargs=2, metavar=("ROOT", "SOCKET_FD"))
    args = parser.parse_args(argv)
    if args.serve_synthetic:
        return _serve_synthetic(Path(args.serve_synthetic[0]), int(args.serve_synthetic[1]))
    if args.report is None:
        parser.error("--report is required")
    try:
        status = run(args.report)
        report = json.loads(args.report.read_text(encoding="utf-8"))
        print(
            json.dumps(
                {
                    "result": report["result"],
                    "scenario_count": report["counts"]["scenarios"],
                    "elapsed_seconds": report["elapsed_seconds"],
                },
                sort_keys=True,
            )
        )
        return status
    except FileExistsError:
        parser.error("report path must be new")
    except Exception as exc:
        print(f"rehearsal runtime QA failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
