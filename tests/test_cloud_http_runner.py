from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path

import uvicorn

from app.participant_storage import ParticipantStudyStore
from app.rehearsal_app import create_app
from app.study_runtime import PrivateStudyRuntime
from app.study_storage import PrivateStudyStore
from scripts.check_cloud_rehearsal_http import run_check


def _write_private(path: Path, value: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def test_runner_drives_real_loopback_app_without_emitting_secrets(tmp_path, capsys):
    root = tmp_path / "cloud-rehearsal"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    _write_private(
        root / ".study-storage.json",
        json.dumps(
            {
                "schema": "youth-law-private-storage.v1",
                "owner_uid": os.getuid(),
                "real_participant_processing_enabled": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )
    control = root / "control"
    control.mkdir(mode=0o700)
    os.chmod(control, 0o700)
    token = "runner-test-token-" + uuid.uuid4().hex + uuid.uuid4().hex
    notice = "Synthetic HTTP runner notice.\n"
    policy = b'{"rehearsal":"fixture","real_participant_admission":false}'
    _write_private(control / "token", (token + "\n").encode("ascii"))
    _write_private(control / "notice.txt", notice.encode("utf-8"))

    participant_store = ParticipantStudyStore(
        root,
        notice_sha256=hashlib.sha256(notice.encode("utf-8")).hexdigest(),
        policy_sha256=hashlib.sha256(policy).hexdigest(),
    )
    runtime = PrivateStudyRuntime(
        PrivateStudyStore(root), participant_store=participant_store, interval_seconds=0.1
    )

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    port = listener.getsockname()[1]
    app = create_app(
        token,
        private_runtime=runtime,
        allowed_origin=f"http://localhost:{port}",
        notice_text=notice,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_config=None,
            access_log=False,
            proxy_headers=False,
            lifespan="on",
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            if not thread.is_alive():
                raise AssertionError("local test server did not start")
            time.sleep(0.02)
        assert server.started

        report_path = root / "http-report.json"
        result = run_check(root, report_path, f"http://localhost:{port}")

        assert result == 0
        assert report_path.stat().st_mode & 0o777 == 0o600
        report_bytes = report_path.read_bytes()
        report = json.loads(report_bytes)
        assert report["status"] == "pass"
        assert report["real_participant_admission"] is False
        assert report["external_model_enabled"] is False
        assert report["http_request_count"] < 200
        assert set(report["synthetic_record_ids"]) == {"withdrawn", "retained"}
        assert (
            report["synthetic_record_ids"]["withdrawn"]
            != report["synthetic_record_ids"]["retained"]
        )
        assert token.encode() not in report_bytes
        assert all(check["result"] == "pass" for check in report["checks"])
        assert participant_store.read_record(report["synthetic_record_ids"]["withdrawn"]) is None
        assert participant_store.read_record(report["synthetic_record_ids"]["retained"]) is not None
        assert token not in capsys.readouterr().out
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
