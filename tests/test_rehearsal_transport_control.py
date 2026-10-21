from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.rehearsal_transport_control import (
    TransportControlServer,
    enabled_from_environment,
)
from app.study_storage import StudyStorageError


class FakeRuntime:
    def __init__(self, *, healthy=True):
        self.healthy = healthy
        self.sweeps = 0

    def _sweep(self):
        self.sweeps += 1
        self.healthy = True


class FakeClient:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    return root


@pytest.fixture
def short_root():
    # macOS AF_UNIX paths are capped at 104 bytes. Production roots are short;
    # pytest's generated paths are not, so select a private short temp location.
    candidates = [Path(tempfile.gettempdir()).resolve()]
    private_tmp = Path("/private/tmp")
    if private_tmp.is_dir():
        candidates.append(private_tmp)
    base = next(
        candidate
        for candidate in candidates
        if len(
            os.fsencode(
                candidate
                / "yl-xxxxxxxx"
                / "root/runtime/http-iam-control.sock"
            )
        )
        < 104
    )
    parent = Path(tempfile.mkdtemp(prefix="yl-", dir=base))
    root = parent / "root"
    root.mkdir(mode=0o700)
    yield root
    shutil.rmtree(parent)


def _request(path: Path, payload: bytes) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(str(path))
        connection.sendall(payload)
        response = bytearray()
        while not response.endswith(b"\n"):
            chunk = connection.recv(1024)
            if not chunk:
                break
            response.extend(chunk)
    return json.loads(response)


def test_control_is_disabled_unless_environment_names_exact_socket(monkeypatch, tmp_path):
    root = _root(tmp_path)
    monkeypatch.delenv("YOUTHLAW_TRANSPORT_CONTROL_SOCKET", raising=False)
    assert enabled_from_environment(root) is False
    assert not (root / "runtime").exists()

    monkeypatch.setenv("YOUTHLAW_TRANSPORT_CONTROL_SOCKET", "1")
    with pytest.raises(StudyStorageError, match="invalid_transport_control"):
        enabled_from_environment(root)
    monkeypatch.setenv(
        "YOUTHLAW_TRANSPORT_CONTROL_SOCKET",
        str(root / "runtime" / "http-iam-control.sock"),
    )
    assert enabled_from_environment(root) is True


def test_control_rejects_unsafe_parent_symlink_and_existing_socket_path(tmp_path, short_root):
    root = _root(tmp_path)
    unsafe = root / "runtime"
    unsafe.mkdir(mode=0o700)
    os.chmod(unsafe, 0o750)
    with pytest.raises(StudyStorageError, match="unsafe_transport_control"):
        TransportControlServer(root, FakeRuntime(), FakeClient())

    link_root = tmp_path / "linked"
    link_root.symlink_to(root, target_is_directory=True)
    with pytest.raises(StudyStorageError):
        TransportControlServer(link_root, FakeRuntime(), FakeClient())

    short_runtime = short_root / "runtime"
    short_runtime.mkdir(mode=0o700)
    stale = short_runtime / "http-iam-control.sock"
    stale.write_text("foreign")
    with pytest.raises(StudyStorageError, match="unsafe_transport_control"):
        TransportControlServer(root, FakeRuntime(), FakeClient())
    assert stale.read_text() == "foreign"


def test_commands_are_bounded_and_return_only_content_free_metadata(short_root):
    root = short_root
    client = FakeClient()
    runtime = FakeRuntime()
    control = TransportControlServer(root, runtime, client)
    wrapped_client = control.start()
    try:
        assert _request(control.path, b'{"command":"status"}\n') == {
            "ok": True,
            "command": "status",
            "pid": os.getpid(),
        }
        assert _request(control.path, b'{"command":"refresh"}\n') == {
            "ok": True,
            "command": "refresh",
        }
        assert client.closed == 1
        assert _request(control.path, b'{"command":"reconcile"}\n') == {
            "ok": True,
            "command": "reconcile",
        }
        assert runtime.sweeps == 1
        assert _request(control.path, b'{"command":"unknown"}\n') == {
            "ok": False,
            "error": "invalid_command",
        }
        assert _request(control.path, b"x" * 257 + b"\n") == {
            "ok": False,
            "error": "request_too_large",
        }
        assert _request(control.path, b'{"command":"status","extra":1}\n') == {
            "ok": False,
            "error": "invalid_command",
        }
        assert wrapped_client._client is client
    finally:
        control.close()


def test_refresh_timeout_does_not_close_client_during_in_flight_sdk_call(short_root):
    root = short_root
    entered = threading.Event()
    release = threading.Event()

    class BlockingClient(FakeClient):
        def query(self):
            entered.set()
            assert release.wait(2)

    client = BlockingClient()
    control = TransportControlServer(
        root, FakeRuntime(), client, refresh_timeout_seconds=0.05
    )
    wrapped = control.start()
    sdk_call = threading.Thread(target=wrapped.query)
    sdk_call.start()
    try:
        assert entered.wait(1)
        assert _request(control.path, b'{"command":"refresh"}\n') == {
            "ok": False,
            "error": "refresh_busy",
        }
        assert client.closed == 0
    finally:
        release.set()
        sdk_call.join(timeout=1)
        control.close()
    assert not sdk_call.is_alive()


def test_start_cleans_bound_socket_when_serving_thread_cannot_start(
    monkeypatch, short_root
):
    control = TransportControlServer(short_root, FakeRuntime(), FakeClient())
    original_start = threading.Thread.start

    def start_or_fail(thread):
        if thread.name == "youthlaw-http-iam-control":
            raise RuntimeError("thread start failed")
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", start_or_fail)
    with pytest.raises(RuntimeError, match="thread start failed"):
        control.start()

    assert control._server is None
    assert control._thread is None
    assert not control.path.exists()


def test_cleanup_leaves_replaced_foreign_socket_path_untouched(short_root):
    root = short_root
    control = TransportControlServer(root, FakeRuntime(), FakeClient())
    control.start()
    control.path.unlink()
    control.path.write_text("foreign")

    control.close()

    assert control.path.read_text() == "foreign"


def test_serve_without_environment_does_not_create_control_socket(monkeypatch, tmp_path):
    from app import cloud_rehearsal, protected_study, rehearsal_app, study_storage

    root = _root(tmp_path)
    original_client = FakeClient()
    participant_store = SimpleNamespace(
        remote_authority=SimpleNamespace(client=original_client)
    )
    observed = {}

    class FakeStore:
        capacity_limits = {}

    monkeypatch.delenv("YOUTHLAW_TRANSPORT_CONTROL_SOCKET", raising=False)
    monkeypatch.setattr(cloud_rehearsal, "open_store", lambda _config: participant_store)
    monkeypatch.setattr(cloud_rehearsal, "backup_is_fresh", lambda *_args: True)
    monkeypatch.setattr(study_storage, "PrivateStudyStore", lambda _root: FakeStore())
    monkeypatch.setattr(protected_study, "read_token_file", lambda _path: "token")
    monkeypatch.setattr(rehearsal_app, "create_app", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(cloud_rehearsal, "_private_bytes", lambda *_args: b"notice")

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: observed.update(kwargs))
    config = {
        "storage_root": str(root),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "dataset_fingerprint": "a" * 64,
    }

    cloud_rehearsal.serve(config)

    assert observed["host"] == "127.0.0.1"
    assert participant_store.remote_authority.client is original_client
    assert not (root / "runtime").exists()
