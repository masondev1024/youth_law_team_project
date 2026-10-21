"""Real TCP and UNIX-socket regression for the cloud IAM rehearsal boundary.

The fake DynamoDB service intentionally keeps an authorization decision for
the lifetime of its HTTP connection. The test therefore proves that the
service's long-lived botocore pool can retain stale allow and deny behavior,
and that the private operator control refreshes that pool in the same Uvicorn
process. No AWS endpoint, model, user data, or external network is involved.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path

from app.cloud_rehearsal import initialize

SOURCE = Path(__file__).resolve().parents[1]
PROBE = SOURCE / "scripts/http_iam_local_probe.py"
TOKEN_FILE_RELATIVE = Path("control/token")
CONTROL_RELATIVE = Path("runtime/http-iam-control.sock")
ORIGIN = "http://localhost:8017"


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="ascii")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="ascii"))


def _wait_until(predicate, timeout: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.025)
    return predicate()


class RehearsalHttp:
    def __init__(self, port: int):
        self.connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        self.cookie = ""

    def close(self) -> None:
        self.connection.close()

    def request(self, method: str, path: str, body: dict | None = None):
        raw = None if body is None else json.dumps(body, separators=(",", ":"))
        headers = {"Host": "localhost:8017"}
        if raw is not None:
            headers.update({"Content-Type": "application/json", "Origin": ORIGIN})
        if self.cookie:
            headers["Cookie"] = self.cookie
        self.connection.request(method, path, body=raw, headers=headers)
        response = self.connection.getresponse()
        payload = response.read()
        parsed = json.loads(payload) if payload else None
        if method == "POST" and path == "/rehearsal/api/login" and response.status == 200:
            cookie = SimpleCookie()
            cookie.load(response.getheader("Set-Cookie", ""))
            assert cookie
            self.cookie = next(iter(cookie.values())).OutputString().split(";", 1)[0]
        return response.status, parsed


def _control(path: Path, command: str) -> dict:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2)
    try:
        client.connect(str(path))
        client.sendall(json.dumps({"command": command}, separators=(",", ":")).encode() + b"\n")
        chunks = []
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        return json.loads(b"".join(chunks))
    finally:
        client.close()


def test_authenticated_http_observes_connection_cached_iam_until_private_refresh(tmp_path):
    temp_root = Path(
        tempfile.mkdtemp(
            prefix="ylh-", dir=Path(tempfile.gettempdir()).resolve()
        )
    )
    # initialize() requires a nonexistent fresh root. Reuse the unique private
    # mkdtemp path after removing only the empty directory it just created.
    temp_root.rmdir()
    root = temp_root
    assert len(os.fsencode(root / CONTROL_RELATIVE)) < 104
    initialized = initialize(root)
    config = {
        "schema": "youth-law-aws-rehearsal.v1",
        "run_id": "ylaw-http-iam-local-test",
        "region": "ap-northeast-2",
        "storage_root": str(root),
        "table": "ylaw-http-iam-test",
        "bucket": "ylaw-http-iam-test-bucket",
        "dataset_fingerprint": initialized["dataset_fingerprint"],
        "port": 8017,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "log_group": "/youthlaw/http-iam-local-test",
        "authority_function": "ylaw-http-iam-writer",
    }
    config_path = tmp_path / "config.json"
    policy_path = tmp_path / "policy.json"
    stats_path = tmp_path / "transport-stats.json"
    ready_path = tmp_path / "ready.json"
    _write_json(config_path, config)
    _write_json(policy_path, {"decision": "allow"})
    log_path = tmp_path / "probe.log"

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(SOURCE)
    environment["YOUTHLAW_TRANSPORT_CONTROL_SOCKET"] = str(
        root / CONTROL_RELATIVE
    )
    control_path = root / CONTROL_RELATIVE
    process = None
    log_stream = log_path.open("w", encoding="utf-8")
    os.chmod(log_path, 0o600)
    http = None
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(PROBE),
                "--config",
                str(config_path),
                "--policy",
                str(policy_path),
                "--stats",
                str(stats_path),
                "--ready",
                str(ready_path),
            ],
            cwd=SOURCE,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
        )
        assert _wait_until(lambda: ready_path.exists() or process.poll() is not None), (
            "Uvicorn did not report readiness within the bounded startup window"
        )
        assert process.poll() is None, "probe exited before Uvicorn became ready"
        ready = _read_json(ready_path)
        assert ready["pid"] == process.pid
        assert _wait_until(lambda: control_path.exists()), "private control socket was not created"

        status = _control(control_path, "status")
        assert status == {"ok": True, "command": "status", "pid": process.pid}

        token = (root / TOKEN_FILE_RELATIVE).read_text(encoding="ascii").strip()
        http = RehearsalHttp(ready["http_port"])
        unauthenticated_status, unauthenticated_body = http.request(
            "GET", "/rehearsal/api/state"
        )
        assert unauthenticated_status == 401
        assert unauthenticated_body == {"error": "unauthorized"}
        login_status, login_state = http.request(
            "POST", "/rehearsal/api/login", {"token": token}
        )
        assert login_status == 200
        assert login_state["session_admission"] is False
        assert login_state["record_origin"] == "developer_rehearsal"
        assert token not in json.dumps(login_state)

        first_status, first_state = http.request("GET", "/rehearsal/api/state")
        assert first_status == 200
        assert first_state["session_admission"] is False
        assert not {"records", "record_ids", "observations", "consents"} & first_state.keys()

        # The fake transport caches the allow decision for its established
        # botocore keep-alive connection, modeling propagation lag explicitly.
        _write_json(policy_path, {"decision": "deny"})
        stale_allow_status, _ = http.request("GET", "/rehearsal/api/state")
        assert stale_allow_status == 200
        assert _wait_until(lambda: _read_json(stats_path).get("stale_allow", 0) >= 1)

        refreshed = _control(control_path, "refresh")
        assert refreshed == {"ok": True, "command": "refresh"}
        denied_status, denied_body = http.request("GET", "/rehearsal/api/state")
        assert denied_status == 503
        assert denied_body == {"error": "storage_unavailable"}
        assert _read_json(stats_path)["deny_responses"] >= 1

        # A newly restored IAM policy cannot change the old connection's cached
        # deny. The explicit refresh plus reconcile must recover this process.
        _write_json(policy_path, {"decision": "allow"})
        stale_deny = _control(control_path, "reconcile")
        assert stale_deny == {"ok": False, "error": "reconcile_failed"}
        still_denied_status, still_denied_body = http.request(
            "GET", "/rehearsal/api/state"
        )
        assert still_denied_status == 503
        assert still_denied_body == {"error": "storage_unavailable"}
        assert _read_json(stats_path)["stale_deny"] >= 1

        refreshed = _control(control_path, "refresh")
        assert refreshed == {"ok": True, "command": "refresh"}
        reconciled = _control(control_path, "reconcile")
        assert reconciled == {"ok": True, "command": "reconcile"}
        recovered_status, recovered_state = http.request("GET", "/rehearsal/api/state")
        assert recovered_status == 200
        assert recovered_state["session_admission"] is False
        assert recovered_state["record_origin"] == "developer_rehearsal"
        assert not {"records", "record_ids", "observations", "consents"} & recovered_state.keys()
        assert _control(control_path, "status")["pid"] == process.pid
    finally:
        if http is not None:
            http.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        log_stream.close()
        shutil.rmtree(root, ignore_errors=True)

    captured_logs = log_path.read_text(encoding="utf-8")
    assert token not in captured_logs if "token" in locals() else True
    assert "AccessDenied" not in captured_logs
    assert _wait_until(lambda: not control_path.exists(), timeout=2.0)
