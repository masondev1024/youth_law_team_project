"""Private SQLite storage for server-stamped formative participant records.

This is a separate namespace from the synthetic preparation store. It stores
only versioned consent flags and one allowlisted observation snapshot; it has
no free-text or direct-identifier fields.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from pydantic import ValidationError

from app.participant_deletion_ledger import ParticipantDeletionLedger
from app.participant_models import (
    NOTICE_VERSION,
    POLICY_VERSION,
    ConsentInput,
    ObservationInput,
)
from app.study_storage import (
    StudyStorageError,
    _check_ancestors,
    _check_mode,
    _check_no_acl,
    _is_uuid4,
    _StoreLock,
    validate_private_storage_root,
)

_RUNTIME_NAME = "participant-runtime-v1"
_DATABASE_NAME = "participant.sqlite3"
_MARKER_NAME = ".participant-storage.json"
_LOCK_NAME = ".participant-storage.lock"
_SERVICE_LOCK_NAME = ".participant-service.lock"
_DB_SCHEMA = 2
_DB_SCHEMA_NAME = "youth-law-participant-records.v2"
_RECORD_TTL = timedelta(days=30)
_BUSY_TIMEOUT_MS = 2000
_STORAGE_ROW_CEILING = 1000
_TASK_IDS = {
    "formative-ux-repayment-01",
    "formative-ux-collection-01",
    "formative-ux-procedure-01",
}
_REQUEST_KEY = re.compile(
    r"^(0|[1-9][0-9]{0,11}):"
    r"([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise StudyStorageError("invalid_clock")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise StudyStorageError("invalid_store")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)
    except (ValueError, TypeError) as exc:
        raise StudyStorageError("invalid_store") from exc


def _canonical_json(value: dict) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise StudyStorageError("invalid_record") from exc


class ParticipantStudyStore:
    """SQLite store isolated from the existing synthetic file store."""

    def __init__(
        self,
        root: Path,
        *,
        notice_sha256: str,
        policy_sha256: str,
        clock: Callable[[], datetime] = _utc_now,
        _allow_ledger_bootstrap: bool = False,
        _defer_recovery: bool = False,
    ):
        self.root = Path(root)
        if not isinstance(notice_sha256, str) or not _SHA256.fullmatch(notice_sha256):
            raise StudyStorageError("invalid_configuration")
        if not isinstance(policy_sha256, str) or not _SHA256.fullmatch(policy_sha256):
            raise StudyStorageError("invalid_configuration")
        self.notice_sha256 = notice_sha256
        self.policy_sha256 = policy_sha256
        self.marker_data = {
            "schema": _DB_SCHEMA_NAME,
            "schema_version": _DB_SCHEMA,
            "owner_uid": os.getuid(),
            "notice_sha256": notice_sha256,
            "policy_sha256": policy_sha256,
            "record_origin": "developer_rehearsal",
            "participant_admission_enabled": False,
        }
        self.clock = clock
        self.runtime = self.root / _RUNTIME_NAME
        self.database = self.runtime / _DATABASE_NAME
        self.marker = self.runtime / _MARKER_NAME
        self.lock_path = self.runtime / _LOCK_NAME
        self.service_lock_path = self.runtime / _SERVICE_LOCK_NAME
        self._allow_ledger_bootstrap = _allow_ledger_bootstrap
        self._skip_ledger_reconcile = True
        self.ledger: ParticipantDeletionLedger | None = None
        self._bootstrap_mode = _allow_ledger_bootstrap
        self._legacy_marker_data: dict | None = None
        if _defer_recovery:
            self._prepare_recovery_state()
        elif self._bootstrap_mode:
            self._prepare_explicit_bootstrap()
        elif self.runtime.exists():
            self._load_existing_marker()
            with self.service_lease(shared=True):
                self._initialize()
        else:
            self._initialize()

    @staticmethod
    def snapshot() -> dict:
        """Safe static service metadata; makes no filesystem or database calls."""
        return {
            "session_admission": False,
            "record_origin": "developer_rehearsal",
            "notice_version": NOTICE_VERSION,
            "policy_version": POLICY_VERSION,
            "storage_row_ceiling": _STORAGE_ROW_CEILING,
        }

    def _validate_root(self) -> None:
        validate_private_storage_root(self.root)

    def _validate_marker(self) -> None:
        self._validate_root()
        try:
            info = self.marker.lstat()
        except OSError as exc:
            raise StudyStorageError("unsafe_marker") from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 4096
        ):
            raise StudyStorageError("unsafe_marker")
        _check_no_acl(self.marker)
        try:
            marker_data = json.loads(self.marker.read_text(encoding="ascii"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise StudyStorageError("invalid_marker") from exc
        if marker_data != self.marker_data:
            raise StudyStorageError("invalid_marker")

    def _validate_file(self, path: Path, *, max_size: int = 64 * 1024 * 1024) -> os.stat_result:
        try:
            info = path.lstat()
        except OSError as exc:
            raise StudyStorageError("unsafe_file") from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > max_size
        ):
            raise StudyStorageError("unsafe_file")
        _check_no_acl(path)
        return info

    def _validate_runtime(self, *, allow_journal: bool = True) -> None:
        self._validate_root()
        _check_ancestors(self.runtime)
        _check_mode(self.runtime)
        _check_no_acl(self.runtime)
        try:
            names = {path.name for path in self.runtime.iterdir()}
        except OSError as exc:
            raise StudyStorageError("filesystem_error") from exc
        expected = {_DATABASE_NAME, _MARKER_NAME, _LOCK_NAME, _SERVICE_LOCK_NAME}
        if allow_journal and (self.runtime / f"{_DATABASE_NAME}-journal").exists():
            expected.add(f"{_DATABASE_NAME}-journal")
        if names != expected:
            raise StudyStorageError("unexpected_file")
        self._validate_marker()
        self._validate_file(self.database)
        self._validate_file(self.lock_path, max_size=0)
        journal = self.runtime / f"{_DATABASE_NAME}-journal"
        if journal.exists():
            self._validate_file(journal)

    def _initialize(self) -> None:
        try:
            self._validate_root()
            fresh_namespace = not self.runtime.exists()
            if fresh_namespace:
                os.mkdir(self.runtime, 0o700)
            _check_ancestors(self.runtime)
            _check_mode(self.runtime)
            _check_no_acl(self.runtime)
            if fresh_namespace:
                self.marker_data["dataset_id"] = str(uuid.uuid4())
                fd = os.open(self.marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as stream:
                    json.dump(self.marker_data, stream, sort_keys=True, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
                fd = os.open(self.service_lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
                fd = os.open(self.database, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
            else:
                if not self.marker.exists():
                    raise StudyStorageError("unsafe_marker")
                if (
                    not self.lock_path.exists()
                    or not self.service_lock_path.exists()
                    or not self.database.exists()
                ):
                    raise StudyStorageError("unsafe_file")
                self._load_existing_marker()
            if fresh_namespace:
                directory_fd = os.open(self.runtime, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            self._validate_runtime()
            with self._locked():
                with self._connect_locked() as connection:
                    version = connection.execute("PRAGMA user_version").fetchone()[0]
                    tables = {
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    if fresh_namespace and version == 0 and not tables:
                        connection.executescript(
                            """
                            CREATE TABLE records (
                                record_id TEXT PRIMARY KEY,
                                request_key_hash TEXT NOT NULL UNIQUE,
                                task_id TEXT NOT NULL,
                                record_origin TEXT NOT NULL,
                                created_at TEXT NOT NULL,
                                expires_at TEXT NOT NULL,
                                consent_json TEXT NOT NULL,
                                observation_json TEXT,
                                version INTEGER NOT NULL CHECK(version >= 1),
                                notice_sha256 TEXT NOT NULL,
                                policy_sha256 TEXT NOT NULL
                            );
                            CREATE TABLE revocations (
                                request_key_hash TEXT PRIMARY KEY,
                                expires_at TEXT NOT NULL
                            );
                            CREATE TABLE dataset_binding (
                                dataset_id TEXT PRIMARY KEY
                            );
                            PRAGMA user_version=2;
                            """
                        )
                        connection.execute(
                            "INSERT INTO dataset_binding(dataset_id) VALUES (?)",
                            (self.marker_data["dataset_id"],),
                        )
                    elif version != _DB_SCHEMA or tables != {
                        "records",
                        "revocations",
                        "dataset_binding",
                    }:
                        raise StudyStorageError("invalid_store")
                    connection.commit()
                    self._audit_locked(connection)
            dataset_fingerprint = ParticipantDeletionLedger.fingerprint(self.marker_data)
            ledger_path = self.root / "participant-deletions-v1"
            if fresh_namespace:
                self.ledger = ParticipantDeletionLedger(
                    self.root, dataset_fingerprint, create=True
                )
            elif self._allow_ledger_bootstrap and not ledger_path.exists():
                self.ledger = ParticipantDeletionLedger(
                    self.root, dataset_fingerprint, create=True
                )
            else:
                self.ledger = ParticipantDeletionLedger(self.root, dataset_fingerprint)
            self._skip_ledger_reconcile = False
            with self._locked():
                with self._connect_locked() as connection:
                    self._audit_locked(connection)
            self._validate_runtime(allow_journal=False)
        except StudyStorageError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            raise StudyStorageError("filesystem_error") from exc

    def _load_existing_marker(self) -> None:
        """Load the stable namespace UUID while keeping policy pins strict."""
        try:
            info = self.marker.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 4096
            ):
                raise StudyStorageError("unsafe_marker")
            _check_no_acl(self.marker)
            data = json.loads(self.marker.read_text(encoding="ascii"))
        except StudyStorageError:
            raise
        except FileNotFoundError as exc:
            raise StudyStorageError("unsafe_marker") from exc
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise StudyStorageError("invalid_marker") from exc
        if type(data) is not dict or "dataset_id" not in data:
            raise StudyStorageError("migration_required")
        expected_base = {
            key: value
            for key, value in self.marker_data.items()
            if key != "dataset_id"
        }
        if (
            set(data) != set(expected_base) | {"dataset_id"}
            or {key: value for key, value in data.items() if key != "dataset_id"}
            != expected_base
        ):
            raise StudyStorageError("invalid_marker")
        if not _is_uuid4(data.get("dataset_id")):
            raise StudyStorageError("invalid_marker")
        self.marker_data = data

    def _prepare_explicit_bootstrap(self) -> None:
        """Read-only preparation; the expected DB digest is checked before migration."""
        self._validate_root()
        if not self.runtime.exists():
            raise StudyStorageError("migration_required")
        _check_ancestors(self.runtime)
        _check_mode(self.runtime)
        _check_no_acl(self.runtime)
        self._validate_file(self.database)
        try:
            marker_info = self.marker.lstat()
            if (
                stat.S_ISLNK(marker_info.st_mode)
                or not stat.S_ISREG(marker_info.st_mode)
                or marker_info.st_nlink != 1
                or marker_info.st_uid != os.getuid()
                or stat.S_IMODE(marker_info.st_mode) != 0o600
                or marker_info.st_size > 4096
            ):
                raise StudyStorageError("unsafe_marker")
            _check_no_acl(self.marker)
            data = json.loads(self.marker.read_text(encoding="ascii"))
        except StudyStorageError:
            raise
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise StudyStorageError("invalid_marker") from exc
        legacy = {
            "schema": "youth-law-participant-records.v1",
            "schema_version": 1,
            "owner_uid": os.getuid(),
            "notice_sha256": self.notice_sha256,
            "policy_sha256": self.policy_sha256,
            "record_origin": "developer_rehearsal",
            "participant_admission_enabled": False,
        }
        if data == legacy:
            self._legacy_marker_data = data
            dataset_id = self._legacy_database_binding()
            self.marker_data = {
                "schema": _DB_SCHEMA_NAME,
                "schema_version": _DB_SCHEMA,
                "owner_uid": os.getuid(),
                "notice_sha256": self.notice_sha256,
                "policy_sha256": self.policy_sha256,
                "record_origin": "developer_rehearsal",
                "participant_admission_enabled": False,
                "dataset_id": dataset_id or str(uuid.uuid4()),
            }
            if dataset_id is not None:
                # A previous offline migration may have committed the SQLite
                # binding before it could replace this marker. Reuse that
                # validated identity so retry remains idempotent.
                connection = sqlite3.connect(
                    self.database.as_uri() + "?mode=ro", uri=True
                )
                connection.row_factory = sqlite3.Row
                try:
                    self._audit_locked(connection)
                finally:
                    connection.close()
            return
        self.marker_data = {
            **{key: value for key, value in self.marker_data.items()},
            "dataset_id": data.get("dataset_id") if type(data) is dict else None,
        }
        expected_base = {
            key: value
            for key, value in self.marker_data.items()
            if key != "dataset_id"
        }
        if (
            type(data) is not dict
            or {key: value for key, value in data.items() if key != "dataset_id"} != expected_base
            or not _is_uuid4(data.get("dataset_id"))
        ):
            raise StudyStorageError("invalid_marker")
        if not self.lock_path.exists():
            raise StudyStorageError("unsafe_lock")

    def _legacy_database_binding(self) -> str | None:
        """Return a valid v2 binding left behind by an interrupted migration."""
        connection = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 1:
                return None
            if version != _DB_SCHEMA:
                raise StudyStorageError("invalid_store")
            objects = {
                (row[0], row[1])
                for row in connection.execute(
                    "SELECT type,name FROM sqlite_master "
                    "WHERE type IN ('table','view','trigger')"
                )
            }
            if objects != {
                ("table", "records"),
                ("table", "revocations"),
                ("table", "dataset_binding"),
            }:
                raise StudyStorageError("invalid_store")
            bindings = [
                row[0] for row in connection.execute("SELECT dataset_id FROM dataset_binding")
            ]
            if len(bindings) != 1 or not _is_uuid4(bindings[0]):
                raise StudyStorageError("invalid_store")
            return bindings[0]
        except sqlite3.Error as exc:
            raise StudyStorageError("invalid_store") from exc
        finally:
            connection.close()

    def _prepare_recovery_state(self) -> None:
        """Validate control files read-only; restore acquires the exclusive fence first."""
        self._load_existing_marker()
        self._validate_runtime(allow_journal=False)
        self.ledger = ParticipantDeletionLedger(
            self.root,
            ParticipantDeletionLedger.fingerprint(self.marker_data),
        )

    def _locked(self):
        return _StoreLock(self.lock_path, self._validate_runtime)

    def current_ledger_checkpoint(self) -> str:
        """Return the recovery watermark under the participant operation lock.

        Ledger verification may complete durable interrupted work, so callers
        must not read its checkpoint outside the same lock used by writes.
        """
        if self.ledger is None:
            raise StudyStorageError("invalid_ledger")
        with self._locked():
            return self.ledger.checkpoint_id()

    @contextmanager
    def service_lease(self, *, shared: bool = False):
        """Hold a distinct process-lifetime fence; request locks remain usable."""
        self._validate_runtime()
        fd = os.open(self.service_lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise StudyStorageError("unsafe_lock")
            _check_no_acl(self.service_lock_path)
            try:
                mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
                fcntl.flock(fd, mode | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StudyStorageError("service_active") from exc
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _reconcile_ledger_locked(self, connection: sqlite3.Connection) -> None:
        if self.ledger is None:
            raise StudyStorageError("invalid_ledger")
        now = self._now()
        entries, _ = self.ledger.verified_entries()
        active: dict[str, str] = {}
        for entry in entries:
            expires = _parse_time(entry["expires_at"])
            if expires > now:
                active[entry["request_key_hash"]] = entry["expires_at"]
        connection.execute("BEGIN IMMEDIATE")
        for request_hash, expires_at in active.items():
            connection.execute("DELETE FROM records WHERE request_key_hash=?", (request_hash,))
            connection.execute(
                "INSERT OR REPLACE INTO revocations(request_key_hash,expires_at) VALUES (?,?)",
                (request_hash, expires_at),
            )
        connection.commit()

    @contextmanager
    def _connect_locked(self) -> Iterator[sqlite3.Connection]:
        """Open a fresh, guarded connection and verify durability settings."""
        self._validate_runtime()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.database,
                timeout=_BUSY_TIMEOUT_MS / 1000,
                isolation_level="DEFERRED",
            )
            connection.row_factory = sqlite3.Row
            journal = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            secure_delete = connection.execute("PRAGMA secure_delete").fetchone()[0]
            synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]
            busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
            if (journal, secure_delete, synchronous, busy_timeout) != (
                "delete",
                1,
                2,
                _BUSY_TIMEOUT_MS,
            ):
                raise StudyStorageError("unsafe_database")
            if not self._skip_ledger_reconcile:
                self._audit_locked(connection)
                self._reconcile_ledger_locked(connection)
            yield connection
            connection.close()
            connection = None
            self._validate_runtime(allow_journal=False)
        except StudyStorageError:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise StudyStorageError("database_error") from exc
        finally:
            if connection is not None:
                connection.close()

    def _now(self) -> datetime:
        return _utc(self.clock())

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA user_version").fetchone()[0] != _DB_SCHEMA:
            raise StudyStorageError("invalid_store")
        objects = {
            (row["type"], row["name"])
            for row in connection.execute(
                "SELECT type,name FROM sqlite_master WHERE type IN ('table','view','trigger')"
            )
        }
        if objects != {
            ("table", "records"),
            ("table", "revocations"),
            ("table", "dataset_binding"),
        }:
            raise StudyStorageError("invalid_store")
        columns = {
            table: {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            for table in ("records", "revocations", "dataset_binding")
        }
        if columns != {
            "records": {
                "record_id",
                "request_key_hash",
                "task_id",
                "record_origin",
                "created_at",
                "expires_at",
                "consent_json",
                "observation_json",
                "version",
                "notice_sha256",
                "policy_sha256",
            },
            "revocations": {"request_key_hash", "expires_at"},
            "dataset_binding": {"dataset_id"},
        }:
            raise StudyStorageError("invalid_store")
        bindings = [row[0] for row in connection.execute("SELECT dataset_id FROM dataset_binding")]
        if bindings != [self.marker_data["dataset_id"]]:
            raise StudyStorageError("wrong_dataset")

    def _audit_locked(self, connection: sqlite3.Connection) -> int:
        """Validate bounded database integrity and every persisted row."""
        self._validate_schema(connection)
        quick_check = [row[0] for row in connection.execute("PRAGMA quick_check")]
        if quick_check != ["ok"]:
            raise StudyStorageError("invalid_store")

        record_count, revocation_count = connection.execute(
            "SELECT (SELECT COUNT(*) FROM records), (SELECT COUNT(*) FROM revocations)"
        ).fetchone()
        total = record_count + revocation_count
        if total > _STORAGE_ROW_CEILING:
            raise StudyStorageError("invalid_store")

        record_hashes: set[str] = set()
        for row in connection.execute("SELECT * FROM records"):
            self._record(row)
            request_hash = row["request_key_hash"]
            if not isinstance(request_hash, str) or not _SHA256.fullmatch(request_hash):
                raise StudyStorageError("invalid_store")
            record_hashes.add(request_hash)

        revocation_hashes: set[str] = set()
        for row in connection.execute("SELECT request_key_hash,expires_at FROM revocations"):
            request_hash, expires_at = row["request_key_hash"], row["expires_at"]
            if not isinstance(request_hash, str) or not _SHA256.fullmatch(request_hash):
                raise StudyStorageError("invalid_store")
            expires = _parse_time(expires_at)
            if _iso(expires) != expires_at:
                raise StudyStorageError("invalid_store")
            revocation_hashes.add(request_hash)

        if record_hashes & revocation_hashes:
            raise StudyStorageError("invalid_store")
        return total

    def _validate_task(self, task_id: object) -> None:
        if type(task_id) is not str or task_id not in _TASK_IDS:
            raise StudyStorageError("invalid_record")

    def _validate_request_key(self, request_key: object) -> str:
        if type(request_key) is not str:
            raise StudyStorageError("invalid_request_key")
        match = _REQUEST_KEY.fullmatch(request_key)
        if match is None:
            raise StudyStorageError("invalid_request_key")
        timestamp = int(match.group(1))
        now = int(self._now().timestamp())
        if timestamp > now + 5 or timestamp <= now - 600:
            raise StudyStorageError("invalid_request_key")
        return hashlib.sha256(request_key.encode("ascii")).hexdigest()

    @staticmethod
    def _validate_consent(consent: dict) -> dict:
        try:
            parsed = ConsentInput.model_validate(consent)
        except (ValidationError, TypeError) as exc:
            raise StudyStorageError("invalid_record") from exc
        value = parsed.model_dump(mode="json")
        if value["notice_version"] != NOTICE_VERSION or value["policy_version"] != POLICY_VERSION:
            raise StudyStorageError("invalid_record")
        return value

    @staticmethod
    def _validate_observation(observation: dict) -> dict:
        try:
            parsed = ObservationInput.model_validate(observation)
        except (ValidationError, TypeError) as exc:
            raise StudyStorageError("invalid_record") from exc
        return parsed.model_dump(mode="json")

    def _record(self, row: sqlite3.Row) -> dict:
        try:
            consent = json.loads(row["consent_json"])
            observation = (
                None if row["observation_json"] is None else json.loads(row["observation_json"])
            )
            if (
                row["record_origin"] != "developer_rehearsal"
                or row["notice_sha256"] != self.notice_sha256
                or row["policy_sha256"] != self.policy_sha256
                or type(row["task_id"]) is not str
                or row["task_id"] not in _TASK_IDS
                or not _is_uuid4(row["record_id"])
                or not isinstance(row["request_key_hash"], str)
                or not _SHA256.fullmatch(row["request_key_hash"])
                or type(row["version"]) is not int
                or row["version"] < 1
            ):
                raise StudyStorageError("invalid_store")
            created, expires = _parse_time(row["created_at"]), _parse_time(row["expires_at"])
            if (
                _iso(created) != row["created_at"]
                or _iso(expires) != row["expires_at"]
                or expires - created != _RECORD_TTL
            ):
                raise StudyStorageError("invalid_store")
            consent = ParticipantStudyStore._validate_consent(consent)
            if not all(
                consent[field]
                for field in (
                    "adult_confirmed",
                    "participation_agreed",
                    "observation_recording_agreed",
                )
            ):
                raise StudyStorageError("invalid_store")
            if observation is not None:
                observation = ParticipantStudyStore._validate_observation(observation)
            if (row["version"] == 1) != (observation is None):
                raise StudyStorageError("invalid_store")
        except StudyStorageError as exc:
            raise StudyStorageError("invalid_store") from exc
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StudyStorageError("invalid_store") from exc
        return {
            "record_id": row["record_id"],
            "task_id": row["task_id"],
            "record_origin": row["record_origin"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "consent": consent,
            "observation": observation,
            "version": row["version"],
            "notice_sha256": row["notice_sha256"],
            "policy_sha256": row["policy_sha256"],
            "observation_provenance": "operator_reported_rehearsal_state",
            "elapsed_seconds_provenance": "operator_observed",
        }

    def create_consent(
        self,
        consent: dict,
        *,
        task_id: str,
        request_key: str | None = None,
        record_origin: str = "developer_rehearsal",
    ) -> dict | None:
        self._validate_task(task_id)
        if record_origin != "developer_rehearsal":
            raise StudyStorageError("admission_closed")
        consent_value = self._validate_consent(consent)
        if not consent_value["adult_confirmed"]:
            raise StudyStorageError("invalid_record")
        if (
            not consent_value["participation_agreed"]
            or not consent_value["observation_recording_agreed"]
        ):
            return None
        if request_key is None:
            raise StudyStorageError("invalid_request_key")
        request_hash = self._validate_request_key(request_key)
        now = self._now()
        created_at, expires_at = _iso(now), _iso(now + _RECORD_TTL)
        with self._locked():
            with self._connect_locked() as connection:
                connection.execute("BEGIN IMMEDIATE")
                total_rows = self._audit_locked(connection)
                if connection.execute(
                    "SELECT 1 FROM revocations WHERE request_key_hash=? AND expires_at>?",
                    (request_hash, _iso(now)),
                ).fetchone():
                    connection.rollback()
                    raise StudyStorageError("revoked")
                existing = connection.execute(
                    "SELECT * FROM records WHERE request_key_hash=?", (request_hash,)
                ).fetchone()
                if existing is not None:
                    existing_record = self._record(existing)
                    if _parse_time(existing["expires_at"]) <= now:
                        connection.rollback()
                        raise StudyStorageError("expired")
                    same = (
                        existing["task_id"] == task_id
                        and existing_record["consent"] == consent_value
                        and existing["notice_sha256"] == self.notice_sha256
                        and existing["policy_sha256"] == self.policy_sha256
                    )
                    if not same:
                        connection.rollback()
                        raise StudyStorageError("conflict")
                    connection.commit()
                    return existing_record
                if total_rows >= _STORAGE_ROW_CEILING:
                    connection.rollback()
                    raise StudyStorageError("capacity")
                record_id = str(uuid.uuid4())
                connection.execute(
                    """INSERT INTO records
                    (record_id,request_key_hash,task_id,record_origin,created_at,expires_at,
                     consent_json,observation_json,version,notice_sha256,policy_sha256)
                    VALUES (?,?,?,?,?,?,?,NULL,1,?,?)""",
                    (
                        record_id,
                        request_hash,
                        task_id,
                        "developer_rehearsal",
                        created_at,
                        expires_at,
                        _canonical_json(consent_value),
                        self.notice_sha256,
                        self.policy_sha256,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM records WHERE record_id=?", (record_id,)
                ).fetchone()
                connection.commit()
                return self._record(row)

    def write_observation(
        self, record_id: str, observation: dict, *, expected_version: int
    ) -> dict:
        if not _is_uuid4(record_id) or type(expected_version) is not int or expected_version < 1:
            raise StudyStorageError("invalid_record")
        observation_value = self._validate_observation(observation)
        now = self._now()
        with self._locked():
            with self._connect_locked() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._audit_locked(connection)
                row = connection.execute(
                    "SELECT * FROM records WHERE record_id=?", (record_id,)
                ).fetchone()
                if row is None:
                    connection.rollback()
                    raise StudyStorageError("expired")
                current = self._record(row)
                if _parse_time(row["expires_at"]) <= now:
                    connection.rollback()
                    raise StudyStorageError("expired")
                if current["observation"] == observation_value:
                    connection.commit()
                    return current
                if current["version"] != expected_version:
                    connection.rollback()
                    raise StudyStorageError("conflict")
                connection.execute(
                    "UPDATE records SET observation_json=?,version=version+1 WHERE record_id=?",
                    (_canonical_json(observation_value), record_id),
                )
                updated = connection.execute(
                    "SELECT * FROM records WHERE record_id=?", (record_id,)
                ).fetchone()
                connection.commit()
                return self._record(updated)

    def read_record(self, record_id: str) -> dict | None:
        if not _is_uuid4(record_id):
            raise StudyStorageError("invalid_record")
        now = self._now()
        with self._locked():
            with self._connect_locked() as connection:
                self._audit_locked(connection)
                row = connection.execute(
                    "SELECT * FROM records WHERE record_id=?", (record_id,)
                ).fetchone()
                if row is None:
                    return None
                record = self._record(row)
                return None if _parse_time(row["expires_at"]) <= now else record

    def list_records(self, limit: int = 20) -> list[dict]:
        """Return a bounded list of unexpired records without consent or observations."""
        if type(limit) is not int or not 1 <= limit <= 20:
            raise StudyStorageError("invalid_record")
        now = _iso(self._now())
        with self._locked():
            with self._connect_locked() as connection:
                self._audit_locked(connection)
                rows = connection.execute(
                    "SELECT record_id,task_id,expires_at,version FROM records "
                    "WHERE expires_at>? ORDER BY created_at DESC,record_id ASC LIMIT ?",
                    (now, limit),
                ).fetchall()
                return [
                    {
                        "record_id": row["record_id"],
                        "task_id": row["task_id"],
                        "expires_at": row["expires_at"],
                        "version": row["version"],
                    }
                    for row in rows
                ]

    def withdraw(self, record_id: str) -> bool:
        if not _is_uuid4(record_id):
            raise StudyStorageError("invalid_record")
        with self._locked():
            with self._connect_locked() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._audit_locked(connection)
                row = connection.execute(
                    "SELECT request_key_hash,expires_at FROM records WHERE record_id=?",
                    (record_id,),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return False
                connection.execute(
                    "INSERT OR REPLACE INTO revocations(request_key_hash,expires_at) VALUES (?,?)",
                    (row["request_key_hash"], row["expires_at"]),
                )
                if self.ledger is None:
                    connection.rollback()
                    raise StudyStorageError("invalid_ledger")
                self.ledger.append(row["request_key_hash"], row["expires_at"], self._now())
                connection.execute("DELETE FROM records WHERE record_id=?", (record_id,))
                connection.commit()
                return True

    def purge_expired(self) -> dict[str, int]:
        now = _iso(self._now())
        with self._locked():
            with self._connect_locked() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._audit_locked(connection)
                records = connection.execute(
                    "DELETE FROM records WHERE expires_at<=?", (now,)
                ).rowcount
                revocations = connection.execute(
                    "DELETE FROM revocations WHERE expires_at<=?", (now,)
                ).rowcount
                connection.commit()
                if self.ledger is not None:
                    self.ledger.compact(self._now())
                return {
                    "participant_records_deleted": records,
                    "participant_revocations_deleted": revocations,
                }

    def summary(self) -> dict[str, int]:
        now = _iso(self._now())
        with self._locked():
            with self._connect_locked() as connection:
                self._audit_locked(connection)
                return {
                    "developer_rehearsal_records": connection.execute(
                        "SELECT COUNT(*) FROM records "
                        "WHERE record_origin='developer_rehearsal' AND expires_at>?",
                        (now,),
                    ).fetchone()[0],
                    "participant_records": 0,
                    "expired_records": connection.execute(
                        "SELECT COUNT(*) FROM records WHERE expires_at<=?", (now,)
                    ).fetchone()[0],
                    "revocations": connection.execute(
                        "SELECT COUNT(*) FROM revocations WHERE expires_at>?", (now,)
                    ).fetchone()[0],
                }
