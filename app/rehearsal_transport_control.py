"""Optional process-local transport control for the isolated IAM rehearsal.

The Unix socket is an operator-local channel. It is deliberately separate from
the participant store namespace and is never exposed as an HTTP route.
"""

from __future__ import annotations

import json
import math
import os
import socket
import socketserver
import stat
import threading
from pathlib import Path
from typing import Any

from app.study_storage import StudyStorageError, _check_ancestors, _check_no_acl

_CONTROL_DIRECTORY = "runtime"
_SOCKET_NAME = "http-iam-control.sock"
_MAX_REQUEST_BYTES = 256
_REQUEST_TIMEOUT_SECONDS = 2.0
_REFRESH_LOCK_TIMEOUT_SECONDS = 0.25


class SerializedTransportClient:
    """Serialize DynamoDB calls against closing its existing transport client."""

    def __init__(self, client: Any, *, refresh_timeout_seconds: float) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._refresh_timeout_seconds = refresh_timeout_seconds

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._client, name)
        if not callable(attribute):
            return attribute

        def serialized_call(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                return attribute(*args, **kwargs)

        return serialized_call

    def close_for_refresh(self) -> bool:
        """Close the original client after active calls finish, within a bound."""
        if not self._lock.acquire(timeout=self._refresh_timeout_seconds):
            return False
        try:
            self._client.close()
            return True
        finally:
            self._lock.release()


def enabled_from_environment(storage_root: Path | None = None) -> bool:
    """Enable only when the env value names this run's exact private socket."""
    value = os.environ.get("YOUTHLAW_TRANSPORT_CONTROL_SOCKET", "")
    if value == "":
        return False
    if storage_root is None:
        raise StudyStorageError("invalid_transport_control")
    expected_path = str(Path(storage_root) / _CONTROL_DIRECTORY / _SOCKET_NAME)
    if value == expected_path:
        return True
    raise StudyStorageError("invalid_transport_control")


def _validate_directory(path: Path) -> None:
    _check_ancestors(path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise StudyStorageError("unsafe_transport_control") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise StudyStorageError("unsafe_transport_control")
    _check_no_acl(path)


class _ControlRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        self.request.settimeout(_REQUEST_TIMEOUT_SECONDS)
        try:
            line = self.rfile.readline(_MAX_REQUEST_BYTES + 1)
        except (OSError, TimeoutError):
            self._respond({"ok": False, "error": "request_timeout"})
            return
        if len(line) > _MAX_REQUEST_BYTES:
            self._respond({"ok": False, "error": "request_too_large"})
            return
        if not line.endswith(b"\n"):
            self._respond({"ok": False, "error": "invalid_command"})
            return
        try:
            request = json.loads(line[:-1].decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._respond({"ok": False, "error": "invalid_command"})
            return
        if type(request) is not dict or set(request) != {"command"}:
            self._respond({"ok": False, "error": "invalid_command"})
            return
        command = request["command"]
        if command == "status":
            self._respond({"ok": True, "command": "status", "pid": os.getpid()})
        elif command == "refresh":
            try:
                refreshed = self.server.transport_client.close_for_refresh()
            except Exception:
                self._respond({"ok": False, "error": "control_failed"})
            else:
                if not refreshed:
                    self._respond({"ok": False, "error": "refresh_busy"})
                else:
                    self._respond({"ok": True, "command": "refresh"})
        elif command == "reconcile":
            try:
                self.server.runtime._sweep()
            except Exception:
                self._respond({"ok": False, "error": "control_failed"})
            else:
                self._respond(
                    {"ok": True, "command": "reconcile"}
                    if self.server.runtime.healthy
                    else {"ok": False, "error": "reconcile_failed"}
                )
        else:
            self._respond({"ok": False, "error": "invalid_command"})

    def _respond(self, value: dict[str, Any]) -> None:
        try:
            self.wfile.write(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
                + b"\n"
            )
            self.wfile.flush()
        except OSError:
            return


class _UnixControlServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 4

    def __init__(self, path: Path, runtime: Any, transport_client: SerializedTransportClient):
        self.runtime = runtime
        self.transport_client = transport_client
        super().__init__(str(path), _ControlRequestHandler, bind_and_activate=False)
        self.server_bind()
        try:
            bound = path.lstat()
            self.socket_identity = (bound.st_dev, bound.st_ino)
            os.chmod(path, 0o600, follow_symlinks=False)
            info = path.lstat()
            if (
                not stat.S_ISSOCK(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or (info.st_dev, info.st_ino) != self.socket_identity
            ):
                raise StudyStorageError("unsafe_transport_control")
            self.server_activate()
        except Exception:
            self.server_close()
            _remove_owned_socket(path, getattr(self, "socket_identity", None))
            raise

    def handle_error(self, _request: socket.socket, _client_address: Any) -> None:
        # Do not send handler tracebacks to an unmanaged output sink.
        return


def _remove_owned_socket(path: Path, identity: tuple[int, int] | None) -> None:
    if identity is None:
        return
    try:
        info = path.lstat()
        if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == identity:
            path.unlink()
    except FileNotFoundError:
        return


class TransportControlServer:
    """Own one private socket and its serving thread for a single Uvicorn run."""

    def __init__(
        self,
        storage_root: Path,
        runtime: Any,
        transport_client: Any,
        *,
        refresh_timeout_seconds: float = _REFRESH_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        if (
            isinstance(refresh_timeout_seconds, bool)
            or not isinstance(refresh_timeout_seconds, (int, float))
            or not math.isfinite(refresh_timeout_seconds)
            or not 0 < refresh_timeout_seconds <= _REQUEST_TIMEOUT_SECONDS
        ):
            raise StudyStorageError("invalid_transport_control")
        self.storage_root = Path(storage_root)
        if not self.storage_root.is_absolute() or self.storage_root != Path(
            os.path.abspath(self.storage_root)
        ):
            raise StudyStorageError("unsafe_transport_control")
        self.runtime = runtime
        self.directory = self.storage_root / _CONTROL_DIRECTORY
        self.path = self.directory / _SOCKET_NAME
        _check_ancestors(self.storage_root)
        _validate_directory(self.storage_root)
        # Unix-domain socket paths have a small platform-specific limit. Fail
        # before creating the control directory when this configured root cannot
        # host the required exact endpoint.
        if len(os.fsencode(self.path)) >= 104:
            raise StudyStorageError("unsafe_transport_control")
        try:
            self.directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise StudyStorageError("unsafe_transport_control") from exc
        _validate_directory(self.directory)
        if self.path.exists() or self.path.is_symlink():
            raise StudyStorageError("unsafe_transport_control")
        self.refresh_timeout_seconds = float(refresh_timeout_seconds)
        self.transport_client = SerializedTransportClient(
            transport_client, refresh_timeout_seconds=self.refresh_timeout_seconds
        )
        self._server: _UnixControlServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> SerializedTransportClient:
        if self._server is not None:
            raise StudyStorageError("invalid_transport_control")
        server = _UnixControlServer(self.path, self.runtime, self.transport_client)
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="youthlaw-http-iam-control",
            daemon=True,
        )
        self._server = server
        self._thread = thread
        try:
            thread.start()
        except BaseException:
            self._server = None
            self._thread = None
            server.server_close()
            _remove_owned_socket(self.path, getattr(server, "socket_identity", None))
            raise
        return self.transport_client

    def close(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=_REQUEST_TIMEOUT_SECONDS + 0.5)
            self._thread = None
        _remove_owned_socket(self.path, getattr(server, "socket_identity", None))
