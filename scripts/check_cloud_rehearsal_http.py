#!/usr/bin/env python3
"""Run a bounded HTTP-only rehearsal flow against the loopback cloud app.

The runner reads the operator token from ``<storage-root>/control/token`` and
never prints or stores credentials, cookies, CSRF values, request bodies, or
response bodies. Its private report contains categorical checks, HTTP counts,
bounded latency summaries, and the synthetic record IDs needed for restore
comparison. It does not call AWS or an external model.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.cookiejar
import json
import os
import re
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_DEFAULT_BASE_URL = "http://localhost:8017"
_TOKEN_LIMIT = 128
_BODY_LIMIT = 1024 * 1024
_REQUEST_LIMIT = 200
_TOKEN_PATTERN = re.compile(r"^[^\s]{32,120}$")
_TASK_ID = "formative-ux-repayment-01"
_CARD = {"issue": "repayment", "selected_facts": ["working", "not_overdue"]}
_OBSERVATION = {
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


class CheckFailure(Exception):
    """Safe failure with no request, response, credential, or exception text."""

    def __init__(self, scenario: str, reason: str):
        self.scenario = scenario
        self.reason = reason
        super().__init__(scenario, reason)


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: float


class LoopbackHttpClient:
    def __init__(self, base_url: str, *, cookies: bool = True, timeout: float = 20.0):
        parts = urlsplit(base_url)
        if (
            parts.scheme != "http"
            or parts.hostname != "localhost"
            or parts.port is None
            or parts.path
            or parts.query
            or parts.fragment
            or parts.username
            or parts.password
            or base_url != f"http://localhost:{parts.port}"
        ):
            raise CheckFailure("configuration", "invalid_loopback_url")
        self.base_url = base_url
        self.origin = base_url
        self.host = parts.netloc
        self.timeout = timeout
        handlers: list[Any] = [urllib.request.ProxyHandler({})]
        if cookies:
            self.cookie_jar = http.cookiejar.CookieJar()
            handlers.append(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        else:
            self.cookie_jar = None
        self.opener = urllib.request.build_opener(*handlers)

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        csrf: str | None = None,
        origin: str | None = None,
    ) -> HttpResult:
        headers = {"Host": self.host}
        if payload is not None:
            headers["Content-Type"] = "application/json"
            headers["Origin"] = origin or self.origin
        if csrf is not None:
            headers["X-Rehearsal-CSRF"] = csrf
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        started = time.monotonic()
        try:
            response = self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            response = error
        except (urllib.error.URLError, TimeoutError, OSError):
            raise CheckFailure("http_transport", "request_failed") from None
        try:
            body = response.read(_BODY_LIMIT + 1)
            if len(body) > _BODY_LIMIT:
                raise CheckFailure("http_response", "response_too_large")
            response_headers = {
                "retry-after": response.headers.get("Retry-After", "")
            }
            status = response.status
        except CheckFailure:
            raise
        except Exception:
            raise CheckFailure("http_response", "response_read_failed") from None
        finally:
            response.close()
        return HttpResult(
            status=status,
            headers=response_headers,
            body=body,
            elapsed_ms=round((time.monotonic() - started) * 1000, 2),
        )


class CloudHttpCheck:
    def __init__(self, storage_root: Path, base_url: str):
        self.base_url = base_url
        self.http_requests = 0
        self._count_lock = threading.Lock()
        self.checks: list[dict[str, Any]] = []
        self.load_checks: dict[str, dict[str, Any]] = {}
        self.record_ids: dict[str, str] = {}

    def _request(self, client: LoopbackHttpClient, *args, **kwargs) -> HttpResult:
        with self._count_lock:
            self.http_requests += 1
            if self.http_requests > _REQUEST_LIMIT:
                raise CheckFailure("request_bound", "request_limit_exceeded")
        return client.request(*args, **kwargs)

    @staticmethod
    def _json(result: HttpResult, scenario: str) -> dict[str, Any]:
        try:
            value = json.loads(result.body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CheckFailure(scenario, "invalid_json") from None
        if not isinstance(value, dict):
            raise CheckFailure(scenario, "invalid_response_shape")
        return value

    def _record(self, scenario: str, passed: bool, *, status: int | None = None) -> None:
        self.checks.append(
            {
                "scenario": scenario,
                "status_code": status,
                "result": "pass" if passed else "fail",
            }
        )
        if not passed:
            raise CheckFailure(scenario, "expectation_mismatch")

    def run(self, token: str) -> dict[str, Any]:
        self._check_readiness()
        self._check_auth_boundaries()
        target_client, csrf, versions, target_id = self._create_observed_record(token)
        keeper_id = self._create_retained_record(token, versions)
        self._check_load()
        self._withdraw_and_verify(target_client, csrf, target_id, keeper_id)
        return self._report("pass", None)

    def _check_readiness(self) -> None:
        client = LoopbackHttpClient(self.base_url, cookies=False)
        for path, scenario in (("/health", "health"), ("/ready", "ready")):
            result = self._request(client, "GET", path)
            payload = self._json(result, scenario)
            passed = (
                result.status == 200
                and payload.get("status") == "ready"
                and payload.get("session_admission") is False
                and payload.get("external_model_enabled") is False
            )
            self._record(scenario, passed, status=result.status)

    def _check_auth_boundaries(self) -> None:
        client = LoopbackHttpClient(self.base_url, cookies=False)
        result = self._request(client, "GET", "/rehearsal/api/state")
        self._record("unauthenticated_state_denied", result.status == 401, status=result.status)

        result = self._request(
            client,
            "POST",
            "/rehearsal/api/login",
            payload={"token": "invalid-token-for-rehearsal-check"},
        )
        self._record("invalid_token_denied", result.status == 401, status=result.status)

        # An unexpected Host is rejected before session or data handling.
        wrong_host = LoopbackHttpClient(self.base_url, cookies=False)
        wrong_host.host = "example.invalid"
        result = self._request(wrong_host, "GET", "/health")
        self._record("wrong_host_denied", result.status == 404, status=result.status)

    def _login(self, token: str) -> tuple[LoopbackHttpClient, str, dict[str, str], dict[str, Any]]:
        client = LoopbackHttpClient(self.base_url)
        response = self._request(
            client, "POST", "/rehearsal/api/login", payload={"token": token}
        )
        body = self._json(response, "valid_login")
        self._record("valid_login", response.status == 200, status=response.status)
        csrf = body.get("csrf_token")
        if not isinstance(csrf, str) or not csrf:
            raise CheckFailure("valid_login", "csrf_missing")
        state = self._request(client, "GET", "/rehearsal/api/state")
        state_body = self._json(state, "authenticated_state")
        self._record("authenticated_state", state.status == 200, status=state.status)
        notice = state_body.get("notice")
        if not isinstance(notice, dict):
            raise CheckFailure("consent_contract", "notice_missing")
        versions = {
            "notice_version": notice.get("notice_version"),
            "policy_version": notice.get("policy_version"),
        }
        if not all(isinstance(value, str) and value for value in versions.values()):
            raise CheckFailure("consent_contract", "version_missing")
        return client, csrf, versions, state_body

    def _consent(self, client: LoopbackHttpClient, csrf: str, versions: dict[str, str]):
        payload = {
            "task_id": _TASK_ID,
            "consent": {
                "adult_confirmed": True,
                "participation_agreed": True,
                "observation_recording_agreed": True,
                **versions,
            },
        }
        result = self._request(
            client, "POST", "/rehearsal/api/consent", payload=payload, csrf=csrf
        )
        state = self._json(result, "consent")
        self._record("synthetic_consent", result.status == 200, status=result.status)
        record = state.get("record")
        record_id = record.get("record_id") if isinstance(record, dict) else None
        if not isinstance(record_id, str) or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            record_id,
        ):
            raise CheckFailure("synthetic_consent", "record_id_missing")
        return state, record_id

    def _create_observed_record(self, token: str):
        client, csrf, versions, _ = self._login(token)
        state, record_id = self._consent(client, csrf, versions)
        self._record(
            "consent_admission_closed",
            state.get("session_admission") is False,
        )
        result = self._request(client, "POST", "/rehearsal/api/start", payload={}, csrf=csrf)
        started = self._json(result, "task_start")
        self._record("task_start", result.status == 200, status=result.status)
        visible = any(
            task.get("scenario")
            for task in started.get("tasks", [])
            if isinstance(task, dict)
        )
        if not visible:
            raise CheckFailure("task_start", "scenario_not_revealed")

        card = self._request(
            client,
            "POST",
            "/api/preparation/card",
            payload=_CARD,
            csrf=csrf,
        )
        card_body = self._json(card, "preparation_card")
        selected = [item.get("id") for item in card_body.get("selected_facts", [])]
        self._record(
            "preparation_card_selection",
            card.status == 200
            and card_body.get("status") == "ready"
            and selected == _CARD["selected_facts"],
            status=card.status,
        )

        observation = self._request(
            client,
            "POST",
            "/rehearsal/api/observations",
            payload={"expected_version": 1, "observation": _OBSERVATION},
            csrf=csrf,
        )
        observation_body = self._json(observation, "observation_storage")
        record = observation_body.get("record")
        self._record(
            "observation_storage",
            observation.status == 200
            and isinstance(record, dict)
            and record.get("record_id") == record_id
            and record.get("version") == 2,
            status=observation.status,
        )
        self.record_ids["withdrawn"] = record_id
        return client, csrf, versions, record_id

    def _create_retained_record(self, token: str, versions: dict[str, str]) -> str:
        client = LoopbackHttpClient(self.base_url)
        response = self._request(
            client, "POST", "/rehearsal/api/login", payload={"token": token}
        )
        self._record("retained_record_login", response.status == 200, status=response.status)
        login_body = self._json(response, "retained_record_login")
        csrf = login_body.get("csrf_token")
        if not isinstance(csrf, str) or not csrf:
            raise CheckFailure("retained_record_login", "csrf_missing")
        _, record_id = self._consent(client, csrf, versions)
        self.record_ids["retained"] = record_id
        client.opener.close() if hasattr(client.opener, "close") else None
        return record_id

    def _check_load(self) -> None:
        def probe() -> tuple[int, str, float]:
            client = LoopbackHttpClient(self.base_url, cookies=False)
            result = self._request(client, "GET", "/health")
            return result.status, result.headers.get("retry-after", ""), result.elapsed_ms

        for concurrency in (1, 4, 8):
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
                results = list(pool.map(lambda _index: probe(), range(concurrency)))
            statuses = Counter(status for status, _, _ in results)
            retry_after_valid = all(
                retry_after == "1"
                for status, retry_after, _ in results
                if status == 429
            )
            passed = all(status in {200, 429} for status, _, _ in results) and retry_after_valid
            self._record(f"bounded_health_concurrency_{concurrency}", passed)
            elapsed = sorted(value for _, _, value in results)
            self.load_checks[str(concurrency)] = {
                "requests": len(results),
                "status_counts": {str(code): count for code, count in sorted(statuses.items())},
                "p50_ms": round(elapsed[(len(elapsed) - 1) // 2], 2),
                "max_ms": round(max(elapsed), 2),
            }

    def _withdraw_and_verify(
        self,
        client: LoopbackHttpClient,
        csrf: str,
        withdrawn_id: str,
        retained_id: str,
    ) -> None:
        result = self._request(
            client, "POST", "/rehearsal/api/withdraw", payload={}, csrf=csrf
        )
        state = self._json(result, "withdrawal")
        available = state.get("available_records", [])
        available_ids = {
            item.get("record_id") for item in available if isinstance(item, dict)
        }
        self._record(
            "withdrawal_keeps_other_record",
            result.status == 200
            and withdrawn_id not in available_ids
            and retained_id in available_ids,
            status=result.status,
        )

        restore = self._request(
            client,
            "POST",
            "/rehearsal/api/restore",
            payload={"record_id": withdrawn_id},
            csrf=csrf,
        )
        self._record(
            "withdrawn_record_restore_denied",
            restore.status == 410,
            status=restore.status,
        )

        final_state = self._request(client, "GET", "/rehearsal/api/state")
        final = self._json(final_state, "post_withdrawal_state")
        available = final.get("available_records", [])
        available_ids = {
            item.get("record_id") for item in available if isinstance(item, dict)
        }
        self._record(
            "post_withdrawal_lookup_closed",
            final_state.status == 200
            and withdrawn_id not in available_ids
            and retained_id in available_ids,
            status=final_state.status,
        )

    def _report(self, status: str, failure: CheckFailure | None) -> dict[str, Any]:
        report: dict[str, Any] = {
            "schema": "cloud-rehearsal-http-check.v1",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": status,
            "base_url": self.base_url,
            "http_request_count": self.http_requests,
            "request_limit": _REQUEST_LIMIT,
            "checks": self.checks,
            "load_checks": self.load_checks,
            "synthetic_record_ids": dict(self.record_ids),
            "real_participant_admission": False,
            "external_model_enabled": False,
        }
        if failure is not None:
            report["failure"] = {"scenario": failure.scenario, "reason": failure.reason}
        return report


def _read_token(storage_root: Path) -> str:
    try:
        root_info = storage_root.lstat()
        control_path = storage_root / "control"
        control_info = control_path.lstat()
        token_path = storage_root / "control" / "token"
        info = token_path.lstat()
    except OSError:
        raise CheckFailure("token_file", "unavailable") from None
    if (
        stat.S_ISLNK(root_info.st_mode)
        or not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != os.getuid()
        or stat.S_IMODE(root_info.st_mode) != 0o700
        or stat.S_ISLNK(control_info.st_mode)
        or not stat.S_ISDIR(control_info.st_mode)
        or control_info.st_uid != os.getuid()
        or stat.S_IMODE(control_info.st_mode) != 0o700
        or stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > _TOKEN_LIMIT
    ):
        raise CheckFailure("token_file", "unsafe_permissions")
    try:
        fd = os.open(token_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_uid != os.getuid()
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_size > _TOKEN_LIMIT
            ):
                raise CheckFailure("token_file", "unsafe_permissions")
            raw = stream.read(_TOKEN_LIMIT + 1)
        if len(raw) > _TOKEN_LIMIT or not raw.endswith(b"\n"):
            raise CheckFailure("token_file", "invalid_format")
        token = raw[:-1].decode("ascii")
    except (OSError, UnicodeDecodeError):
        raise CheckFailure("token_file", "unreadable") from None
    if not _TOKEN_PATTERN.fullmatch(token):
        raise CheckFailure("token_file", "invalid_format")
    return token


def _write_report(path: Path, report: dict[str, Any]) -> None:
    if not path.is_absolute():
        raise CheckFailure("report_file", "path_must_be_absolute")
    parent = path.parent
    try:
        parent_info = parent.lstat()
    except OSError:
        raise CheckFailure("report_file", "parent_unavailable") from None
    if (
        stat.S_ISLNK(parent_info.st_mode)
        or not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.getuid()
        or stat.S_IMODE(parent_info.st_mode) != 0o700
    ):
        raise CheckFailure("report_file", "parent_not_private")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            os.close(fd)
            raise CheckFailure("report_file", "unsafe_file")
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise CheckFailure("report_file", "already_exists") from None
    except OSError:
        raise CheckFailure("report_file", "write_failed") from None


def _preflight_report_destination(path: Path) -> None:
    """Reject unusable report paths and confirm private space before HTTP writes."""
    if not path.is_absolute():
        raise CheckFailure("report_file", "path_must_be_absolute")
    parent = path.parent
    try:
        parent_info = parent.lstat()
    except OSError:
        raise CheckFailure("report_file", "parent_unavailable") from None
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        raise CheckFailure("report_file", "path_unavailable") from None
    else:
        raise CheckFailure("report_file", "already_exists")
    if (
        stat.S_ISLNK(parent_info.st_mode)
        or not stat.S_ISDIR(parent_info.st_mode)
        or parent_info.st_uid != os.getuid()
        or stat.S_IMODE(parent_info.st_mode) != 0o700
    ):
        raise CheckFailure("report_file", "parent_not_private")

    probe = parent / f".{path.name}.preflight-{uuid.uuid4().hex}"
    try:
        fd = os.open(
            probe,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(b"\0" * 65536)
            stream.flush()
            os.fsync(stream.fileno())
        probe.unlink()
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        raise CheckFailure("report_file", "not_writable") from None


def run_check(storage_root: Path, report_path: Path, base_url: str) -> int:
    started = time.monotonic()
    checker = CloudHttpCheck(storage_root, base_url)
    failure: CheckFailure | None = None
    try:
        if not storage_root.is_absolute():
            raise CheckFailure("configuration", "storage_root_must_be_absolute")
        _preflight_report_destination(report_path)
        token = _read_token(storage_root)
        checker.run(token)
    except CheckFailure as exc:
        failure = exc
    except Exception:
        # Keep unexpected library/server exceptions out of reports and stdout.
        failure = CheckFailure("runner", "unexpected_failure")
    report = checker._report("fail" if failure else "pass", failure)
    report["duration_seconds"] = round(time.monotonic() - started, 2)
    try:
        _write_report(report_path, report)
    except CheckFailure as exc:
        print(f"FAIL scenario={exc.scenario} reason={exc.reason}", file=sys.stderr)
        return 2
    print(
        f"{report['status'].upper()} checks={len(checker.checks)} "
        f"http_requests={checker.http_requests} report_written=true"
    )
    if failure is not None:
        print(f"FAIL scenario={failure.scenario} reason={failure.reason}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--base-url", default=_DEFAULT_BASE_URL)
    parser.add_argument("--phase", choices=("flow",), default="flow")
    args = parser.parse_args(argv)
    return run_check(args.storage_root, args.report, args.base_url)


if __name__ == "__main__":
    raise SystemExit(main())
