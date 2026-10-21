"""Bounded, private local storage for synthetic preparation-study metadata.

This module deliberately has no connection to the legacy participant or
administration stores. Callers pass already-minimized synthetic observations
and allowlisted operational metadata only.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import math
import os
import re
import stat
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Callable

_MARKER_NAME = ".study-storage.json"
_SCHEMA = "youth-law-private-storage.v1"
_RUNTIME_NAME = "app-runtime-v1"
_OBS_TTL = timedelta(days=30)
_LOG_TTL = timedelta(hours=24)
OBSERVATION_CAPACITY = 2000
LOG_CAPACITY = 10000
CAPACITY_LIMITS = MappingProxyType({"observations": OBSERVATION_CAPACITY, "logs": LOG_CAPACITY})
_MAX_BYTES = 16 * 1024
_LOCK_TIMEOUT = 2.0
_UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_TASK_IDS = {
    "formative-ux-repayment-01",
    "formative-ux-collection-01",
    "formative-ux-procedure-01",
}
_COMPLETIONS = {"completed", "stopped", "technical_failure"}
_LOG_EVENTS = {"preparation_request", "protected_study_request", "server_diagnostic"}
_LOG_ROUTES = {
    "root",
    "prepare",
    "health",
    "options",
    "card",
    "metrics",
    "observations",
    "participant_records",
    "retention",
    "other",
    "static",
    "/",
    "/prepare",
    "/health",
    "/api/preparation/options",
    "/api/preparation/card",
}
_EXCEPTION_CLASSES = {"RuntimeError", "ValueError", "TypeError", "OSError", "UnexpectedError"}
_THREAD_LOCK = threading.Lock()
_ACL_TYPE_EXTENDED = 0x00000100
_ACL_FIRST_ENTRY = 0
_MAC_ACL_LIBRARY = None


class StudyStorageError(Exception):
    """Safe categorized storage error; never includes path or record content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(dt: datetime) -> datetime:
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise StudyStorageError("invalid_clock")
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return _utc(dt).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_iso(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise StudyStorageError("invalid_store")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise StudyStorageError("invalid_store") from exc
    return _utc(parsed)


def _is_uuid4(value: object) -> bool:
    return isinstance(value, str) and bool(_UUID4.fullmatch(value))


def _check_mode(path: Path, expected: int = 0o700) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise StudyStorageError("filesystem_error") from exc
    if stat.S_ISLNK(info.st_mode):
        raise StudyStorageError("unsafe_path")
    if info.st_uid != os.getuid():
        raise StudyStorageError("unsafe_owner")
    if stat.S_IMODE(info.st_mode) != expected:
        raise StudyStorageError("unsafe_mode")
    return info


def _check_ancestors(path: Path) -> None:
    """Reject symlinked ancestors; parent ownership may be system/root."""
    absolute = Path(os.path.abspath(path))
    chain = [absolute, *absolute.parents]
    for item in chain:
        try:
            info = item.lstat()
        except OSError as exc:
            raise StudyStorageError("unsafe_path") from exc
        if stat.S_ISLNK(info.st_mode):
            raise StudyStorageError("unsafe_path")


def _load_mac_acl_library():
    global _MAC_ACL_LIBRARY
    if _MAC_ACL_LIBRARY is None:
        try:
            library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            library.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
            library.acl_get_fd_np.restype = ctypes.c_void_p
            library.acl_get_entry.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            library.acl_get_entry.restype = ctypes.c_int
            library.acl_free.argtypes = [ctypes.c_void_p]
            library.acl_free.restype = ctypes.c_int
            _MAC_ACL_LIBRARY = library
        except (AttributeError, OSError) as exc:
            raise StudyStorageError("acl_unverifiable") from exc
    return _MAC_ACL_LIBRARY


def _mac_acl_present(fd: int) -> bool:
    """Return whether an open file descriptor has an extended ACL."""
    library = _load_mac_acl_library()
    ctypes.set_errno(0)
    acl = library.acl_get_fd_np(fd, _ACL_TYPE_EXTENDED)
    if not acl:
        # On macOS a valid descriptor with no extended ACL returns ENOENT.
        # Other errors cannot be distinguished safely from an inspection failure.
        if ctypes.get_errno() == errno.ENOENT:
            return False
        raise StudyStorageError("acl_unverifiable")
    try:
        entry = ctypes.c_void_p()
        ctypes.set_errno(0)
        result = library.acl_get_entry(acl, _ACL_FIRST_ENTRY, ctypes.byref(entry))
        if result == 0:
            return True
        raise StudyStorageError("acl_unverifiable")
    finally:
        if library.acl_free(acl) != 0:
            raise StudyStorageError("acl_unverifiable")


def _check_no_acl(path: Path) -> None:
    """Inspect ACLs through fd/native APIs and fail closed on ambiguity."""
    try:
        if os.uname().sysname == "Darwin":
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                if _mac_acl_present(fd):
                    raise StudyStorageError("acl_present")
            finally:
                os.close(fd)
            return
        for attr in ("system.posix_acl_access", "system.posix_acl_default"):
            try:
                os.getxattr(path, attr, follow_symlinks=False)
            except OSError as exc:
                if exc.errno in (errno.ENODATA, getattr(errno, "ENOATTR", errno.ENODATA)):
                    continue
                raise StudyStorageError("acl_unverifiable") from exc
            raise StudyStorageError("acl_present")
    except StudyStorageError:
        raise
    except (OSError, AttributeError, ctypes.ArgumentError) as exc:
        raise StudyStorageError("acl_unverifiable") from exc


def _json_bytes(value: object) -> bytes:
    try:
        data = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise StudyStorageError("invalid_record") from exc
    if len(data) > _MAX_BYTES:
        raise StudyStorageError("invalid_record")
    return data


class PrivateStudyStore:
    """Private file store rooted at a pre-created, explicitly marked directory."""

    CAPACITY_LIMITS = CAPACITY_LIMITS
    OBSERVATION_CAPACITY = OBSERVATION_CAPACITY
    LOG_CAPACITY = LOG_CAPACITY

    def __init__(self, root: Path, *, clock: Callable[[], datetime] = _utc_now):
        self.root = Path(root)
        self.clock = clock
        self.runtime = self.root / _RUNTIME_NAME
        self.observations = self.runtime / "observations"
        self.logs = self.runtime / "logs"
        self.lock_path = self.runtime / ".study-storage.lock"
        self._initialize()

    @property
    def capacity_limits(self) -> dict[str, int]:
        """Current configured per-kind hard limits for runtime health snapshots."""
        return dict(CAPACITY_LIMITS)

    def _initialize(self) -> None:
        try:
            self._validate_root_marker()

            # The store creates only its own dedicated subtree, one level at a time.
            if not self.runtime.exists():
                os.mkdir(self.runtime, 0o700)
            _check_ancestors(self.runtime)
            _check_mode(self.runtime)
            for directory in (self.observations, self.logs):
                if not directory.exists():
                    os.mkdir(directory, 0o700)
                _check_mode(directory)
                _check_no_acl(directory)
            if not self.lock_path.exists():
                try:
                    fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    os.close(fd)
                    self._fsync_dir(self.runtime)
                except FileExistsError:
                    pass
            lock_info = self.lock_path.lstat()
            if (
                stat.S_ISLNK(lock_info.st_mode)
                or not stat.S_ISREG(lock_info.st_mode)
                or lock_info.st_nlink != 1
                or lock_info.st_uid != os.getuid()
                or stat.S_IMODE(lock_info.st_mode) != 0o600
            ):
                raise StudyStorageError("unsafe_lock")
            _check_no_acl(self.lock_path)
            self._check_runtime_contents()
            # A prior process may have stopped between durable temp creation and
            # publication. Remove only exact temp names while holding our lock.
            with self._locked():
                self._check_directory_contents(self.observations, full_audit=True)
                self._check_directory_contents(self.logs, full_audit=True)
                self._recover_temps()
        except StudyStorageError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise StudyStorageError("filesystem_error") from exc

    def _validate_root_marker(self) -> None:
        validate_private_storage_root(self.root)

    def _check_runtime_contents(self) -> None:
        _check_ancestors(self.runtime)
        _check_mode(self.runtime)
        _check_no_acl(self.runtime)
        try:
            names = {entry.name for entry in self.runtime.iterdir()}
        except OSError as exc:
            raise StudyStorageError("filesystem_error") from exc
        expected = {"observations", "logs", ".study-storage.lock"}
        if names != expected:
            raise StudyStorageError("unexpected_file")

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _secure_file(path: Path, *, check_acl: bool = True) -> os.stat_result:
        try:
            info = path.lstat()
        except OSError as exc:
            raise StudyStorageError("filesystem_error") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StudyStorageError("unsafe_file")
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise StudyStorageError("unsafe_file")
        if info.st_size > _MAX_BYTES:
            raise StudyStorageError("invalid_store")
        if check_acl:
            _check_no_acl(path)
        return info

    def _check_directory_contents(self, directory: Path, *, full_audit: bool = False) -> None:
        children = list(directory.iterdir())
        linked_targets: set[str] = set()
        for child in children:
            if child.name.startswith(".tmp-") and _is_uuid4(child.name[5:]):
                if self._validate_temp(child, directory).st_nlink == 2:
                    linked_targets.add(child.name[5:] + ".json")
        for child in children:
            if child.name.startswith(".tmp-") and _is_uuid4(child.name[5:]):
                continue
            if not child.name.endswith(".json") or not _is_uuid4(child.name[:-5]):
                raise StudyStorageError("unexpected_file")
            if child.name not in linked_targets:
                self._secure_file(child, check_acl=full_audit)

    def _validate_temp(self, path: Path, directory: Path) -> os.stat_result:
        try:
            info = path.lstat()
        except OSError as exc:
            raise StudyStorageError("filesystem_error") from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > _MAX_BYTES
        ):
            raise StudyStorageError("unsafe_file")
        _check_no_acl(path)
        if info.st_nlink == 2:
            # Crash after atomic hard-link publication but before temp unlink.
            final = directory / f"{path.name[5:]}.json"
            try:
                final_info = final.lstat()
            except OSError as exc:
                raise StudyStorageError("unsafe_file") from exc
            if (
                stat.S_ISLNK(final_info.st_mode)
                or not stat.S_ISREG(final_info.st_mode)
                or final_info.st_uid != os.getuid()
                or stat.S_IMODE(final_info.st_mode) != 0o600
                or final_info.st_size > _MAX_BYTES
            ):
                raise StudyStorageError("unsafe_file")
            _check_no_acl(final)
            if final_info.st_ino != info.st_ino or final_info.st_dev != info.st_dev:
                raise StudyStorageError("unsafe_file")
        elif info.st_nlink != 1:
            raise StudyStorageError("unsafe_file")
        return info

    def _now(self) -> datetime:
        return _utc(self.clock())

    def _locked(self):
        return _StoreLock(self.lock_path, self._validate_before_lock)

    def _validate_before_lock(self) -> None:
        self._validate_root_marker()
        self._check_runtime_contents()

    def _recover_temps(self) -> None:
        # Only our exact recognizable temp-name format is eligible for cleanup.
        for directory in (self.observations, self.logs):
            for child in directory.iterdir():
                if child.name.startswith(".tmp-") and _is_uuid4(child.name[5:]):
                    self._validate_temp(child, directory)
                    child.unlink()
                    self._fsync_dir(directory)

    def _prepare_locked(self) -> None:
        self._check_runtime_contents()
        for directory in (self.observations, self.logs):
            _check_mode(directory)
            _check_no_acl(directory)
        self._check_directory_contents(self.observations)
        self._check_directory_contents(self.logs)
        self._recover_temps()

    def _read_record(self, path: Path, kind: str, *, check_acl: bool = True) -> dict:
        self._secure_file(path, check_acl=check_acl)
        try:
            raw = path.read_bytes()
            record = json.loads(raw)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise StudyStorageError("invalid_store") from exc
        if not isinstance(record, dict) or set(record) != {
            "schema",
            "kind",
            "id",
            "created_at",
            "expires_at",
            "payload",
        }:
            raise StudyStorageError("invalid_store")
        if record["schema"] != "youth-law-study-record.v1" or record["kind"] != kind:
            raise StudyStorageError("invalid_store")
        if record["id"] != path.stem or not _is_uuid4(record["id"]):
            raise StudyStorageError("invalid_store")
        created, expires = _parse_iso(record["created_at"]), _parse_iso(record["expires_at"])
        ttl = _OBS_TTL if kind == "observation" else _LOG_TTL
        if expires - created != ttl:
            raise StudyStorageError("invalid_store")
        if kind == "observation":
            self._validate_observation_payload(record["payload"])
        else:
            self._validate_log_payload(record["payload"])
        return record

    @staticmethod
    def _validate_observation_payload(payload: object) -> dict:
        if not isinstance(payload, dict) or set(payload) != {
            "scenario",
            "consent",
            "task_id",
            "completion",
            "elapsed_seconds",
        }:
            raise StudyStorageError("invalid_record")
        if (
            type(payload.get("scenario")) is not str
            or payload["scenario"] != "synthetic"
            or type(payload.get("consent")) is not str
            or payload["consent"] != "simulated_granted"
        ):
            raise StudyStorageError("invalid_record")
        if (
            type(payload.get("task_id")) is not str
            or payload["task_id"] not in _TASK_IDS
            or type(payload.get("completion")) is not str
            or payload["completion"] not in _COMPLETIONS
        ):
            raise StudyStorageError("invalid_record")
        elapsed = payload.get("elapsed_seconds")
        if type(elapsed) is not int or not 0 <= elapsed <= 900:
            raise StudyStorageError("invalid_record")
        return payload

    @staticmethod
    def _validate_log_payload(payload: object) -> dict:
        if not isinstance(payload, dict):
            raise StudyStorageError("invalid_record")
        required = {"event", "request_id", "route", "status", "duration_ms"}
        if set(payload) not in (required, required | {"exception_class"}):
            raise StudyStorageError("invalid_record")
        if (
            type(payload["event"]) is not str
            or payload["event"] not in _LOG_EVENTS
            or not _is_uuid4(payload["request_id"])
        ):
            raise StudyStorageError("invalid_record")
        if (
            type(payload["route"]) is not str
            or payload["route"] not in _LOG_ROUTES
            or type(payload["status"]) is not int
            or not 100 <= payload["status"] <= 599
        ):
            raise StudyStorageError("invalid_record")
        duration = payload["duration_ms"]
        if type(duration) not in (int, float) or not 0 <= duration <= 60000:
            raise StudyStorageError("invalid_record")
        if isinstance(duration, float) and not math.isfinite(duration):
            raise StudyStorageError("invalid_record")
        if "exception_class" in payload and (
            type(payload["exception_class"]) is not str
            or payload["exception_class"] not in _EXCEPTION_CLASSES
        ):
            raise StudyStorageError("invalid_record")
        return payload

    def _write(
        self, directory: Path, kind: str, payload: dict, record_id: str | None = None
    ) -> dict:
        validate = (
            self._validate_observation_payload
            if kind == "observation"
            else self._validate_log_payload
        )
        validate(payload)
        now = self._now()
        if record_id is None:
            record_id = str(uuid.uuid4())
        if not _is_uuid4(record_id):
            raise StudyStorageError("invalid_record")
        target = directory / f"{record_id}.json"
        if target.exists():
            existing = self._read_record(target, kind)
            if existing["payload"] != payload:
                raise StudyStorageError("conflict")
            if now >= _parse_iso(existing["expires_at"]):
                raise StudyStorageError("expired")
            return existing
        capacity = OBSERVATION_CAPACITY if kind == "observation" else LOG_CAPACITY
        if sum(1 for item in directory.iterdir() if item.name.endswith(".json")) >= capacity:
            raise StudyStorageError("capacity")
        ttl = _OBS_TTL if kind == "observation" else _LOG_TTL
        envelope = {
            "schema": "youth-law-study-record.v1",
            "kind": kind,
            "id": record_id,
            "created_at": _iso(now),
            "expires_at": _iso(now + ttl),
            "payload": payload,
        }
        data = _json_bytes(envelope)
        temp = directory / f".tmp-{record_id}"
        fd = -1
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                fd = -1
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _check_no_acl(temp)
            os.link(temp, target, follow_symlinks=False)
            temp.unlink()
            self._fsync_dir(directory)
        except FileExistsError:
            if target.exists():
                existing = self._read_record(target, kind)
                if existing["payload"] == payload:
                    return existing
                raise StudyStorageError("conflict")
            raise StudyStorageError("filesystem_error")
        except OSError as exc:
            raise StudyStorageError("filesystem_error") from exc
        finally:
            if fd >= 0:
                os.close(fd)
        return envelope

    def write_observation(self, payload: dict, *, record_id: str | None = None) -> dict:
        with self._locked():
            self._prepare_locked()
            return self._write(self.observations, "observation", payload, record_id)

    def read_observation(self, record_id: str) -> dict | None:
        with self._locked():
            if not _is_uuid4(record_id):
                raise StudyStorageError("invalid_record")
            self._prepare_locked()
            path = self.observations / f"{record_id}.json"
            if not path.exists():
                return None
            record = self._read_record(path, "observation")
            return None if self._now() >= _parse_iso(record["expires_at"]) else record

    def delete_observation(self, record_id: str) -> bool:
        with self._locked():
            if not _is_uuid4(record_id):
                raise StudyStorageError("invalid_record")
            self._prepare_locked()
            path = self.observations / f"{record_id}.json"
            if not path.exists():
                return False
            self._secure_file(path)
            try:
                path.unlink()
                self._fsync_dir(self.observations)
            except OSError as exc:
                raise StudyStorageError("filesystem_error") from exc
            return True

    def write_log(self, payload: dict) -> None:
        with self._locked():
            self._prepare_locked()
            self._write(self.logs, "log", payload)

    def purge_expired(self) -> dict:
        with self._locked():
            self._prepare_locked()
            now = self._now()
            removed = {"observations_deleted": 0, "logs_deleted": 0}
            for kind, directory, count_key in (
                ("observation", self.observations, "observations_deleted"),
                ("log", self.logs, "logs_deleted"),
            ):
                for path in directory.iterdir():
                    if path.name.startswith(".tmp-"):
                        continue
                    record = self._read_record(path, kind)
                    if now >= _parse_iso(record["expires_at"]):
                        try:
                            path.unlink()
                            self._fsync_dir(directory)
                        except OSError as exc:
                            raise StudyStorageError("filesystem_error") from exc
                        removed[count_key] += 1
            return removed

    def summary(self) -> dict:
        with self._locked():
            self._prepare_locked()
            now = self._now()
            result = {"observations": 0, "logs": 0, "observations_expired": 0, "logs_expired": 0}
            for kind, directory in (("observation", self.observations), ("log", self.logs)):
                for path in directory.iterdir():
                    if path.name.startswith(".tmp-"):
                        continue
                    record = self._read_record(path, kind, check_acl=False)
                    expired = now >= _parse_iso(record["expires_at"])
                    stem = "observations" if kind == "observation" else "logs"
                    result[stem + ("_expired" if expired else "")] += 1
            return result


class _StoreLock:
    def __init__(self, path: Path, validate_before_open: Callable[[], None]):
        self.path = path
        self.validate_before_open = validate_before_open
        self.fd: int | None = None

    def __enter__(self):
        deadline = time.monotonic() + _LOCK_TIMEOUT
        self.validate_before_open()
        if not _THREAD_LOCK.acquire(timeout=_LOCK_TIMEOUT):
            raise StudyStorageError("lock_timeout")
        try:
            self.fd = os.open(self.path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            info = os.fstat(self.fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise StudyStorageError("unsafe_lock")
            _check_no_acl(self.path)
            while True:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise StudyStorageError("lock_timeout")
                    time.sleep(0.01)
        except Exception:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            _THREAD_LOCK.release()
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.fd is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
        finally:
            self.fd = None
            _THREAD_LOCK.release()


def validate_private_storage_root(root: Path) -> None:
    """Validate the existing private root marker without creating any layout."""
    root = Path(root)
    if not root.is_absolute():
        raise StudyStorageError("unsafe_path")
    _check_ancestors(root)
    _check_mode(root)
    _check_no_acl(root)
    marker = root / _MARKER_NAME
    try:
        info = marker.lstat()
    except OSError as exc:
        raise StudyStorageError("unsafe_marker") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise StudyStorageError("unsafe_marker")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise StudyStorageError("unsafe_marker")
    _check_no_acl(marker)
    if info.st_size > 4096:
        raise StudyStorageError("invalid_marker")
    try:
        with marker.open("rb") as stream:
            marker_data = json.loads(stream.read(4097))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise StudyStorageError("invalid_marker") from exc
    if (
        not isinstance(marker_data, dict)
        or marker_data.get("schema") != _SCHEMA
        or marker_data.get("owner_uid") != os.getuid()
        or marker_data.get("real_participant_processing_enabled") is not False
    ):
        raise StudyStorageError("invalid_marker")
