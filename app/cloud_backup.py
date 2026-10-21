"""Version-pinned S3 backups for the private participant SQLite store."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.participant_deletion_ledger import ParticipantDeletionLedger
from app.study_storage import (
    StudyStorageError,
    _check_ancestors,
    _check_mode,
    _check_no_acl,
)

_MAX_DATABASE_BYTES = 64 * 1024 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024
_CHUNK_BYTES = 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DATA_KEY = re.compile(r"^backups/([0-9a-f]{64})/([0-9a-f]{32})\.sqlite3$")
_MANIFEST_FIELDS = {
    "dataset_fingerprint",
    "dataset_id",
    "owner_uid",
    "notice_sha256",
    "policy_sha256",
    "created_at",
    "db_sha256",
    "size_bytes",
    "db_key",
    "version_id",
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_time(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)
    except (TypeError, ValueError):
        return False
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z") == value


def _timestamp() -> str:
    return _utc_now().isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json_bytes(value: dict[str, Any]) -> bytes:
    try:
        raw = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise StudyStorageError("invalid_backup_manifest") from exc
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise StudyStorageError("invalid_backup_manifest")
    return raw


@dataclass(frozen=True)
class BackupManifest:
    dataset_fingerprint: str
    dataset_id: str
    owner_uid: int
    notice_sha256: str
    policy_sha256: str
    created_at: str
    db_sha256: str
    size_bytes: int
    db_key: str
    version_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> "BackupManifest":
        if type(value) is not dict or set(value) != _MANIFEST_FIELDS:
            raise StudyStorageError("invalid_backup_manifest")
        manifest = cls(**value)
        manifest.validate()
        return manifest

    def validate(self) -> None:
        if (
            not isinstance(self.dataset_fingerprint, str)
            or not _SHA256.fullmatch(self.dataset_fingerprint)
            or not isinstance(self.dataset_id, str)
            or not re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                self.dataset_id,
            )
            or type(self.owner_uid) is not int
            or self.owner_uid < 0
            or not isinstance(self.notice_sha256, str)
            or not _SHA256.fullmatch(self.notice_sha256)
            or not isinstance(self.policy_sha256, str)
            or not _SHA256.fullmatch(self.policy_sha256)
            or not _canonical_time(self.created_at)
            or not isinstance(self.db_sha256, str)
            or not _SHA256.fullmatch(self.db_sha256)
            or type(self.size_bytes) is not int
            or not 1 <= self.size_bytes <= _MAX_DATABASE_BYTES
            or not isinstance(self.version_id, str)
            or not self.version_id
            or self.version_id == "null"
            or len(self.version_id) > 1024
        ):
            raise StudyStorageError("invalid_backup_manifest")
        match = _DATA_KEY.fullmatch(self.db_key) if isinstance(self.db_key, str) else None
        if match is None or match.group(1) != self.dataset_fingerprint:
            raise StudyStorageError("invalid_backup_manifest")
        self.to_dict()  # Ensure the complete representation stays within the receipt bound.


@dataclass(frozen=True)
class RestoreResult:
    records_withdrawn: int
    expired_records_removed: int
    source_sha256: str
    version_id: str


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(_CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError as exc:
        raise StudyStorageError("filesystem_error") from exc
    return digest.hexdigest()


def _check_workdir(store: Any, workdir: Path) -> Path:
    path = Path(workdir)
    if not path.is_absolute():
        raise StudyStorageError("unsafe_path")
    _check_ancestors(path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise StudyStorageError("unsafe_path") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise StudyStorageError("unsafe_path")
    _check_mode(path, 0o700)
    _check_no_acl(path)
    runtime = Path(os.path.abspath(store.runtime))
    absolute = Path(os.path.abspath(path))
    if absolute == runtime or runtime in absolute.parents:
        raise StudyStorageError("unsafe_path")
    return path


def _check_regular_private(path: Path, *, max_size: int) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise StudyStorageError("unsafe_backup") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > max_size
    ):
        raise StudyStorageError("unsafe_backup")
    _check_no_acl(path)
    return info


def _create_private_file(path: Path) -> int:
    try:
        return os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise StudyStorageError("filesystem_error") from exc


def _snapshot_authority(store: Any) -> object:
    authority = getattr(store, "remote_authority", None)
    if authority is None:
        return None
    authority.assert_current()
    intents, highwater = authority.snapshot()
    normalized = tuple(
        sorted(
            (
                intent.request_key_hash,
                intent.expires_at,
                intent.sequence,
            )
            for intent in intents
        )
    )
    if type(highwater) is not int or highwater < 0:
        raise StudyStorageError("invalid_authority")
    return normalized, highwater


def _dataset_fingerprint(store: Any) -> str:
    if not isinstance(store, object) or type(getattr(store, "marker_data", None)) is not dict:
        raise StudyStorageError("invalid_store")
    try:
        return ParticipantDeletionLedger.fingerprint(store.marker_data)
    except (TypeError, ValueError) as exc:
        raise StudyStorageError("invalid_store") from exc


def _check_manifest_for_store(store: Any, manifest: BackupManifest) -> None:
    manifest.validate()
    marker = store.marker_data
    if (
        manifest.dataset_fingerprint != _dataset_fingerprint(store)
        or manifest.dataset_id != marker.get("dataset_id")
        or manifest.owner_uid != marker.get("owner_uid")
        or manifest.notice_sha256 != marker.get("notice_sha256")
        or manifest.policy_sha256 != marker.get("policy_sha256")
    ):
        raise StudyStorageError("wrong_dataset")


def _atomic_receipt(
    workdir: Path,
    manifest: BackupManifest,
    manifest_key: str,
    manifest_version: str,
) -> Path:
    payload = {
        "manifest": manifest.to_dict(),
        "manifest_key": manifest_key,
        "manifest_version_id": manifest_version,
    }
    raw = _json_bytes(payload)
    receipt = workdir / f"{manifest.db_key.rsplit('/', 1)[1][:-8]}.receipt.json"
    temporary = workdir / f".{uuid.uuid4().hex}.receipt.tmp"
    fd = _create_private_file(temporary)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, receipt)
        directory_fd = os.open(workdir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise StudyStorageError("filesystem_error") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    _check_regular_private(receipt, max_size=_MAX_MANIFEST_BYTES)
    return receipt


def create_backup(store: Any, s3: Any, bucket: str, workdir: Path) -> BackupManifest:
    """Create a consistent SQLite snapshot and upload its pinned S3 version.

    The S3 bucket must have versioning enabled. A unique key and If-None-Match
    make each object write-once; the database VersionId is part of the strict
    manifest. A local receipt is atomically written only after both uploads.
    """
    if not isinstance(bucket, str) or not bucket:
        raise StudyStorageError("invalid_configuration")
    directory = _check_workdir(store, workdir)
    if getattr(store, "ledger", None) is None or getattr(store, "_skip_ledger_reconcile", True):
        raise StudyStorageError("invalid_store")
    fingerprint = _dataset_fingerprint(store)
    backup_id = uuid.uuid4().hex
    db_key = f"backups/{fingerprint}/{backup_id}.sqlite3"
    manifest_key = f"backups/{fingerprint}/{backup_id}.json"
    local_db = directory / f".{backup_id}.sqlite3.tmp"
    destination = None
    try:
        with store._locked():
            remote_before = _snapshot_authority(store)
            # _connect_locked validates the database and reconciles the local
            # deletion journal before creating the backup view.
            with store._connect_locked() as source:
                fd = _create_private_file(local_db)
                os.close(fd)
                destination = sqlite3.connect(local_db)
                destination.row_factory = sqlite3.Row
                try:
                    source.backup(destination)
                    store._audit_locked(destination)
                    destination.commit()
                finally:
                    destination.close()
                    destination = None
            _check_regular_private(local_db, max_size=_MAX_DATABASE_BYTES)
            db_size = local_db.stat().st_size
            if not 1 <= db_size <= _MAX_DATABASE_BYTES:
                raise StudyStorageError("invalid_backup_size")
            digest = _hash_file(local_db)
            remote_after = _snapshot_authority(store)
            if remote_before != remote_after:
                raise StudyStorageError("snapshot_changed")
            created_at = _timestamp()
            manifest = BackupManifest(
                dataset_fingerprint=fingerprint,
                dataset_id=store.marker_data["dataset_id"],
                owner_uid=store.marker_data["owner_uid"],
                notice_sha256=store.marker_data["notice_sha256"],
                policy_sha256=store.marker_data["policy_sha256"],
                created_at=created_at,
                db_sha256=digest,
                size_bytes=db_size,
                db_key=db_key,
                version_id="pending",
            )
        try:
            with local_db.open("rb") as body:
                response = s3.put_object(
                    Bucket=bucket,
                    Key=db_key,
                    Body=body,
                    ContentType="application/vnd.sqlite3",
                    ServerSideEncryption="AES256",
                    IfNoneMatch="*",
                )
        except Exception as exc:
            raise StudyStorageError("backup_unavailable") from exc
        version_id = response.get("VersionId") if isinstance(response, dict) else None
        if not isinstance(version_id, str) or not version_id or version_id == "null":
            raise StudyStorageError("bucket_versioning_required")
        manifest = BackupManifest(**{**manifest.to_dict(), "version_id": version_id})
        manifest.validate()
        raw_manifest = _json_bytes(manifest.to_dict())
        try:
            manifest_response = s3.put_object(
                Bucket=bucket,
                Key=manifest_key,
                Body=raw_manifest,
                ContentType="application/json",
                ServerSideEncryption="AES256",
                IfNoneMatch="*",
            )
        except Exception as exc:
            raise StudyStorageError("backup_unavailable") from exc
        manifest_version = (
            manifest_response.get("VersionId")
            if isinstance(manifest_response, dict)
            else None
        )
        if (
            not isinstance(manifest_version, str)
            or not manifest_version
            or manifest_version == "null"
        ):
            raise StudyStorageError("bucket_versioning_required")
        _atomic_receipt(directory, manifest, manifest_key, manifest_version)
        return manifest
    except StudyStorageError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise StudyStorageError("backup_failed") from exc
    finally:
        if destination is not None:
            destination.close()
        for path in (local_db,):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _download_version(s3: Any, bucket: str, manifest: BackupManifest, target: Path) -> None:
    try:
        response = s3.get_object(
            Bucket=bucket, Key=manifest.db_key, VersionId=manifest.version_id
        )
    except Exception as exc:
        error_response = getattr(exc, "response", None)
        error_code = (
            error_response.get("Error", {}).get("Code")
            if isinstance(error_response, dict)
            else None
        )
        if isinstance(exc, KeyError) or error_code in {"NoSuchVersion", "InvalidArgument"}:
            raise StudyStorageError("invalid_backup_version") from exc
        raise StudyStorageError("backup_unavailable") from exc
    if type(response) is not dict or response.get("VersionId") != manifest.version_id:
        raise StudyStorageError("invalid_backup_version")
    size = response.get("ContentLength")
    body = response.get("Body")
    if type(size) is not int or size != manifest.size_bytes or body is None:
        raise StudyStorageError("invalid_backup_size")
    digest = hashlib.sha256()
    received = 0
    try:
        fd = _create_private_file(target)
        with os.fdopen(fd, "wb") as output:
            while True:
                try:
                    chunk = body.read(_CHUNK_BYTES)
                except Exception as exc:
                    raise StudyStorageError("backup_unavailable") from exc
                if not chunk:
                    break
                received += len(chunk)
                if received > _MAX_DATABASE_BYTES or received > manifest.size_bytes:
                    raise StudyStorageError("invalid_backup_size")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
    except StudyStorageError:
        target.unlink(missing_ok=True)
        raise
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise StudyStorageError("filesystem_error") from exc
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
    if received != manifest.size_bytes:
        target.unlink(missing_ok=True)
        raise StudyStorageError("invalid_backup_size")
    if digest.hexdigest() != manifest.db_sha256:
        target.unlink(missing_ok=True)
        raise StudyStorageError("backup_checksum_mismatch")
    _check_regular_private(target, max_size=_MAX_DATABASE_BYTES)


def restore_backup(
    store: Any,
    s3: Any,
    bucket: str,
    manifest: BackupManifest | dict[str, Any],
    workdir: Path,
) -> RestoreResult:
    """Fetch one S3 object version, verify it, and use fenced recovery install."""
    if not isinstance(bucket, str) or not bucket:
        raise StudyStorageError("invalid_configuration")
    if isinstance(manifest, dict):
        manifest = BackupManifest.from_dict(manifest)
    if not isinstance(manifest, BackupManifest):
        raise StudyStorageError("invalid_backup_manifest")
    _check_manifest_for_store(store, manifest)
    directory = _check_workdir(store, workdir)
    downloaded = directory / f".{uuid.uuid4().hex}.restore.tmp"
    try:
        _download_version(s3, bucket, manifest, downloaded)
        from app.participant_recovery import restore_database

        checkpoint = store.current_ledger_checkpoint()
        result = restore_database(
            store,
            downloaded,
            expected_ledger_checkpoint=checkpoint,
        )
        return RestoreResult(
            records_withdrawn=result["records_withdrawn"],
            expired_records_removed=result["expired_records_removed"],
            source_sha256=result["source_sha256"],
            version_id=manifest.version_id,
        )
    except StudyStorageError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise StudyStorageError("restore_failed") from exc
    finally:
        try:
            downloaded.unlink(missing_ok=True)
        except OSError:
            pass
