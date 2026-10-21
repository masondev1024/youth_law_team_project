"""Offline, fenced recovery for the private participant SQLite database.

The ledger protects withdrawals during database rollback. Its checkpoint is a
local owner-level freshness anchor; rolling both the ledger and checkpoint back
under the same OS owner is outside this mechanism's protection.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import uuid
from contextlib import ExitStack
from pathlib import Path

from app.participant_deletion_ledger import ParticipantDeletionLedger
from app.participant_storage import ParticipantStudyStore, _iso, _parse_time
from app.study_storage import (
    StudyStorageError,
    _check_ancestors,
    _check_mode,
    _check_no_acl,
    _StoreLock,
)

_STAGING_NAME = ".participant-recovery-v1"
_STAGE_FILE = "staged.sqlite3"
_STAGE_ENTRY = re.compile(
    r"(?P<uuid>[0-9a-f]{32})-staged\.sqlite3(?:-(?:journal|wal|shm))?\Z"
)


def _safe_source(path: Path, target: Path) -> tuple[os.stat_result, str]:
    path = Path(path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise StudyStorageError("unsafe_backup") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 64 * 1024 * 1024
        or (info.st_dev, info.st_ino) == (target.stat().st_dev, target.stat().st_ino)
    ):
        raise StudyStorageError("unsafe_backup")
    _check_ancestors(path)
    _check_no_acl(path)
    digest = _sha256(path)
    return info, digest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise StudyStorageError("filesystem_error") from exc
    return digest.hexdigest()


def _same_source(path: Path, original: os.stat_result, digest: str) -> None:
    current = path.lstat()
    if (
        (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        != (original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns)
        or _sha256(path) != digest
    ):
        raise StudyStorageError("backup_changed")


def _stage_root(root: Path) -> Path:
    path = root / _STAGING_NAME
    try:
        _check_ancestors(path.parent)
        if path.exists():
            _check_mode(path)
            _check_no_acl(path)
            if list(path.iterdir()):
                raise StudyStorageError("recovery_staging_not_empty")
        else:
            os.mkdir(path, 0o700)
            _check_mode(path)
            _check_no_acl(path)
        return path
    except StudyStorageError:
        raise
    except OSError as exc:
        raise StudyStorageError("filesystem_error") from exc


def _stage_entry_name_is_known(name: str) -> bool:
    match = _STAGE_ENTRY.fullmatch(name)
    if match is None:
        return False
    try:
        parsed = uuid.UUID(hex=match.group("uuid"))
    except ValueError:
        return False
    return parsed.version == 4 and parsed.variant == uuid.RFC_4122


def _stage_file_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
    )


def cleanup_orphan_staging(store: ParticipantStudyStore) -> int:
    """Remove only fully validated abandoned restore files under both recovery fences."""
    stage_root = store.root / _STAGING_NAME
    with ExitStack() as stack:
        stack.enter_context(store.service_lease())
        stack.enter_context(store._locked())
        try:
            directory_info = stage_root.lstat()
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise StudyStorageError("unsafe_recovery_staging") from exc
        if not stat.S_ISDIR(directory_info.st_mode):
            raise StudyStorageError("unsafe_recovery_staging")
        try:
            _check_ancestors(stage_root)
            _check_mode(stage_root, 0o700)
            _check_no_acl(stage_root)
            directory_fd = os.open(
                stage_root,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except StudyStorageError:
            raise
        except OSError as exc:
            raise StudyStorageError("unsafe_recovery_staging") from exc

        try:
            opened_info = os.fstat(directory_fd)
            if (
                not stat.S_ISDIR(opened_info.st_mode)
                or opened_info.st_uid != os.getuid()
                or stat.S_IMODE(opened_info.st_mode) != 0o700
                or (opened_info.st_dev, opened_info.st_ino)
                != (directory_info.st_dev, directory_info.st_ino)
            ):
                raise StudyStorageError("unsafe_recovery_staging")
            try:
                names = os.listdir(directory_fd)
            except OSError as exc:
                raise StudyStorageError("unsafe_recovery_staging") from exc

            validated: dict[str, tuple[int, ...]] = {}
            for name in names:
                if not _stage_entry_name_is_known(name):
                    raise StudyStorageError("unsafe_recovery_staging")
                try:
                    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError as exc:
                    raise StudyStorageError("unsafe_recovery_staging") from exc
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > 64 * 1024 * 1024
                ):
                    raise StudyStorageError("unsafe_recovery_staging")
                try:
                    _check_no_acl(stage_root / name)
                except StudyStorageError as exc:
                    raise StudyStorageError("unsafe_recovery_staging") from exc
                validated[name] = _stage_file_identity(info)

            removed = 0
            for name, identity in validated.items():
                try:
                    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError as exc:
                    raise StudyStorageError("unsafe_recovery_staging") from exc
                if _stage_file_identity(current) != identity or not stat.S_ISREG(
                    current.st_mode
                ):
                    raise StudyStorageError("unsafe_recovery_staging")
                try:
                    os.unlink(name, dir_fd=directory_fd)
                except OSError as exc:
                    raise StudyStorageError("filesystem_error") from exc
                removed += 1
            if removed:
                os.fsync(directory_fd)
            return removed
        finally:
            os.close(directory_fd)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _apply_ledger(store: ParticipantStudyStore, connection: sqlite3.Connection) -> tuple[int, int]:
    if store.ledger is None:
        raise StudyStorageError("invalid_ledger")
    entries, _ = store.ledger.verified_entries()
    now = store._now()
    active = {
        entry["request_key_hash"]: entry["expires_at"]
        for entry in entries
        if _parse_time(entry["expires_at"]) > now
    }
    expired_records = connection.execute(
        "DELETE FROM records WHERE expires_at<=?", (_iso(now),)
    ).rowcount
    deleted = 0
    for request_hash, expires_at in active.items():
        deleted += connection.execute(
            "DELETE FROM records WHERE request_key_hash=?", (request_hash,)
        ).rowcount
        connection.execute(
            "INSERT OR REPLACE INTO revocations(request_key_hash,expires_at) VALUES (?,?)",
            (request_hash, expires_at),
        )
    connection.execute("DELETE FROM revocations WHERE expires_at<=?", (_iso(now),))
    return deleted, expired_records


def restore_database(
    store: ParticipantStudyStore,
    source_database: Path,
    *,
    expected_ledger_checkpoint: str,
) -> dict[str, int | str]:
    """Apply current deletion history to a staged backup and atomically install it."""
    source = Path(source_database)
    source_info, source_hash = _safe_source(source, store.database)
    if store.ledger is None:
        raise StudyStorageError("invalid_ledger")
    stage_root = None
    stage = None
    try:
        with ExitStack() as stack:
            stack.enter_context(store.service_lease())
            stack.enter_context(store._locked())
            store.ledger.require_checkpoint(expected_ledger_checkpoint)
            stage_root = _stage_root(store.root)
            stage = stage_root / f"{uuid.uuid4().hex}-{_STAGE_FILE}"
            _same_source(source, source_info, source_hash)
            fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output, source.open("rb") as input_stream:
                shutil.copyfileobj(input_stream, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(stage, 0o600)
            stage_info = stage.lstat()
            if (
                not stat.S_ISREG(stage_info.st_mode)
                or stage_info.st_nlink != 1
                or stage_info.st_uid != os.getuid()
                or stat.S_IMODE(stage_info.st_mode) != 0o600
            ):
                raise StudyStorageError("unsafe_backup")
            _check_no_acl(stage)
            connection = sqlite3.connect(stage, timeout=2.0, isolation_level="DEFERRED")
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA secure_delete=ON")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                store._audit_locked(connection)
                withdrawn, expired = _apply_ledger(store, connection)
                store._audit_locked(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
            with stage.open("rb") as stream:
                os.fsync(stream.fileno())
            _same_source(source, source_info, source_hash)
            os.replace(stage, store.database)
            _fsync_dir(store.runtime)
            store._validate_runtime(allow_journal=False)
            return {
                "records_withdrawn": withdrawn,
                "expired_records_removed": expired,
                "source_sha256": source_hash,
            }
    except StudyStorageError:
        raise
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise StudyStorageError("recovery_failed") from exc
    finally:
        try:
            if stage is not None and stage.exists():
                stage.unlink()
                assert stage_root is not None
                _fsync_dir(stage_root)
        except OSError:
            pass


def bootstrap_ledger(
    store: ParticipantStudyStore,
    *,
    expected_db_sha256: str,
    confirm_legacy_services_stopped: bool = False,
) -> dict[str, int]:
    """Explicit offline v1-to-v2 migration and ledger seed from a trusted DB."""
    if (
        not isinstance(expected_db_sha256, str)
        or len(expected_db_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_db_sha256)
    ):
        raise StudyStorageError("invalid_configuration")
    if _sha256(store.database) != expected_db_sha256:
        raise StudyStorageError("stale_database")
    if store._legacy_marker_data is not None and not confirm_legacy_services_stopped:
        raise StudyStorageError("legacy_services_must_be_stopped")
    service_lock = store.service_lock_path
    if not service_lock.exists():
        fd = os.open(service_lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    _check_mode(service_lock, 0o600)
    _check_no_acl(service_lock)
    lease_fd = os.open(service_lock, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        try:
            fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StudyStorageError("service_active") from exc
        with _StoreLock(store.lock_path, lambda: None):
            if _sha256(store.database) != expected_db_sha256:
                raise StudyStorageError("stale_database")
            connection = sqlite3.connect(store.database, timeout=2.0, isolation_level="DEFERRED")
            connection.row_factory = sqlite3.Row
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version == 1 and store._legacy_marker_data is not None:
                    objects = {
                        (row["type"], row["name"])
                        for row in connection.execute(
                            "SELECT type,name FROM sqlite_master "
                            "WHERE type IN ('table','view','trigger')"
                        )
                    }
                    legacy_objects = {("table", "records"), ("table", "revocations")}
                    if objects != legacy_objects:
                        raise StudyStorageError("invalid_store")

                    def validate_v1(conn):
                        if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
                            raise StudyStorageError("invalid_store")
                        names = {
                            row[0]
                            for row in conn.execute(
                                "SELECT name FROM sqlite_master WHERE type='table'"
                            )
                        }
                        if names != {"records", "revocations"}:
                            raise StudyStorageError("invalid_store")

                    original_validate_schema = store._validate_schema
                    store._validate_schema = validate_v1
                    try:
                        store._audit_locked(connection)
                    finally:
                        store._validate_schema = original_validate_schema
                    revocations = [
                        (row["request_key_hash"], row["expires_at"])
                        for row in connection.execute(
                            "SELECT request_key_hash,expires_at FROM revocations "
                            "ORDER BY request_key_hash"
                        )
                    ]
                    connection.execute(
                        "CREATE TABLE dataset_binding(dataset_id TEXT PRIMARY KEY)"
                    )
                    connection.execute(
                        "INSERT INTO dataset_binding(dataset_id) VALUES (?)",
                        (store.marker_data["dataset_id"],),
                    )
                    connection.execute("PRAGMA user_version=2")
                    connection.commit()
                    _write_marker(store)
                    store._legacy_marker_data = None
                else:
                    store._audit_locked(connection)
                    revocations = [
                        (row["request_key_hash"], row["expires_at"])
                        for row in connection.execute(
                            "SELECT request_key_hash,expires_at FROM revocations "
                            "ORDER BY request_key_hash"
                        )
                    ]
                if store.ledger is None:
                    store.ledger = ParticipantDeletionLedger(
                        store.root,
                        ParticipantDeletionLedger.fingerprint(store.marker_data),
                        create=not (store.root / "participant-deletions-v1").exists(),
                    )
                store.ledger.seed(revocations, store._now())
                if store._legacy_marker_data is not None:
                    # Resume the case where SQLite committed v2 but marker
                    # publication was interrupted on an earlier attempt.
                    _write_marker(store)
                    store._legacy_marker_data = None
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
        return {"active_revocations_seeded": len(store.ledger.active_entries(store._now()))}
    finally:
        try:
            fcntl.flock(lease_fd, fcntl.LOCK_UN)
        finally:
            os.close(lease_fd)


def _write_marker(store: ParticipantStudyStore) -> None:
    temporary = store.runtime / ".participant-storage.bootstrap"
    if temporary.exists():
        raise StudyStorageError("unsafe_marker")
    data = json.dumps(store.marker_data, sort_keys=True, separators=(",", ":")).encode("ascii")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, store.marker)
        _fsync_dir(store.runtime)
    except OSError as exc:
        raise StudyStorageError("filesystem_error") from exc


def _store_from_args(
    args: argparse.Namespace, *, bootstrap: bool = False, recovery: bool = False
) -> ParticipantStudyStore:
    from app.participant_policy import load_participant_policy

    digests = load_participant_policy(args.storage_root, args.notice_file, args.policy_file)
    return ParticipantStudyStore(
        args.storage_root,
        **digests,
        _allow_ledger_bootstrap=bootstrap,
        _defer_recovery=recovery,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("checkpoint", "restore", "cleanup-staging", "bootstrap")
    )
    parser.add_argument("--storage-root", type=Path, required=True)
    parser.add_argument("--notice-file", type=Path, required=True)
    parser.add_argument("--policy-file", type=Path, required=True)
    parser.add_argument("--source-database", type=Path)
    parser.add_argument("--expected-ledger-checkpoint")
    parser.add_argument("--expected-db-sha256")
    parser.add_argument("--confirm-legacy-services-stopped", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "checkpoint":
            store = _store_from_args(args)
            print(store.current_ledger_checkpoint())
        elif args.action == "restore":
            if args.source_database is None or args.expected_ledger_checkpoint is None:
                raise StudyStorageError("invalid_configuration")
            store = _store_from_args(args, recovery=True)
            result = restore_database(
                store,
                args.source_database,
                expected_ledger_checkpoint=args.expected_ledger_checkpoint,
            )
            print(
                "restore complete: "
                f"withdrawn={result['records_withdrawn']} "
                f"expired={result['expired_records_removed']}"
            )
        elif args.action == "cleanup-staging":
            store = _store_from_args(args, recovery=True)
            removed = cleanup_orphan_staging(store)
            print(f"staging cleanup complete: files_removed={removed}")
        else:
            if args.expected_db_sha256 is None:
                raise StudyStorageError("invalid_configuration")
            store = _store_from_args(args, bootstrap=True)
            result = bootstrap_ledger(
                store,
                expected_db_sha256=args.expected_db_sha256,
                confirm_legacy_services_stopped=args.confirm_legacy_services_stopped,
            )
            print(
                "ledger bootstrap complete: "
                f"active_revocations={result['active_revocations_seeded']}"
            )
        return 0
    except Exception:
        print("participant recovery failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
