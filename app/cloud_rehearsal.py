"""Isolated AWS rehearsal entry point; never opens recruitment or invokes a model."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import sys
import urllib.request
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.cloud_policy import NOTICE, POLICY, RELEASE
from app.participant_deletion_ledger import ParticipantDeletionLedger
from app.participant_policy import _private_bytes, load_participant_policy
from app.participant_storage import ParticipantStudyStore
from app.study_storage import StudyStorageError, _check_ancestors, _check_no_acl

_SCHEMA = "youth-law-aws-rehearsal.v1"
_FIELDS = {
    "schema",
    "run_id",
    "region",
    "storage_root",
    "table",
    "bucket",
    "dataset_fingerprint",
    "port",
    "expires_at",
    "log_group",
    "authority_function",
}


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def _read_private(path: Path, maximum: int = 65536) -> bytes:
    _check_ancestors(path)
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_uid != os.getuid()
        or info.st_size > maximum
    ):
        raise StudyStorageError("unsafe_cloud_config")
    _check_no_acl(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise StudyStorageError("unsafe_cloud_config")
        data = source.read(maximum + 1)
    if len(data) > maximum:
        raise StudyStorageError("unsafe_cloud_config")
    return data


def _deadline(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise StudyStorageError("invalid_cloud_config") from None


def read_config(path: Path) -> dict:
    data = json.loads(_read_private(path))
    if (
        type(data) is not dict
        or set(data) != _FIELDS
        or data["schema"] != _SCHEMA
        or data["region"] != "ap-northeast-2"
        or data["port"] != 8017
        or not re.fullmatch(r"[a-z0-9-]{8,48}", data["run_id"])
        or not re.fullmatch(r"[0-9a-f]{64}", data["dataset_fingerprint"])
        or not Path(data["storage_root"]).is_absolute()
        or not re.fullmatch(r"[a-z0-9-]{3,63}", data["bucket"])
        or not re.fullmatch(r"[A-Za-z0-9_.-]{3,255}", data["table"])
        or not isinstance(data["log_group"], str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", data["authority_function"])
    ):
        raise StudyStorageError("invalid_cloud_config")
    if _deadline(data["expires_at"]) <= datetime.now(timezone.utc):
        raise StudyStorageError("run_expired")
    return data


def instance_identity() -> tuple[str, str]:
    """Use IMDSv2 directly; never trust an identity copied from an old disk."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        "http://169.254.169.254/latest/api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
    )
    with opener.open(request, timeout=2) as response:
        token = response.read(4096).decode("ascii")
    request = urllib.request.Request(
        "http://169.254.169.254/latest/dynamic/instance-identity/document",
        headers={"X-aws-ec2-metadata-token": token},
    )
    with opener.open(request, timeout=2) as response:
        data = json.loads(response.read(16384))
    if (
        not re.fullmatch(r"i-[0-9a-f]{17}", data.get("instanceId", ""))
        or data.get("region") != "ap-northeast-2"
    ):
        raise StudyStorageError("invalid_instance_identity")
    return data["instanceId"], data["region"]


def aws_client(service: str, region: str):
    import boto3
    from botocore.config import Config

    os.environ["AWS_CONFIG_FILE"] = "/etc/aws/config"
    session = boto3.Session(profile_name="youthlaw-app")
    return session.client(
        service,
        region_name=region,
        # A synchronous Lambda may run for 10 seconds. Wait longer than that
        # bound; a shorter SDK timeout makes a committed cold call ambiguous.
        config=Config(
            connect_timeout=3,
            read_timeout=15 if service == "lambda" else 5,
            retries={"total_max_attempts": 1},
        ),
    )


def initialize(root: Path, *, dataset_id: str | None = None) -> dict:
    """Create a fresh synthetic root only; existing stores are never replaced."""
    if not root.is_absolute() or root.exists():
        raise StudyStorageError("fresh_root_required")
    _check_ancestors(root.parent)
    root.mkdir(mode=0o700)
    _write_new(
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
    _write_new(control / "notice.txt", NOTICE)
    _write_new(control / "policy.json", POLICY)
    _write_new(control / "token", (secrets.token_urlsafe(48) + "\n").encode())
    digests = load_participant_policy(
        root, control / "notice.txt", control / "policy.json", release=RELEASE
    )
    options = {} if dataset_id is None else {"_initial_dataset_id": dataset_id}
    store = ParticipantStudyStore(root, **digests, **options)
    return {
        "dataset_fingerprint": ParticipantDeletionLedger.fingerprint(store.marker_data),
        "dataset_id": store.marker_data["dataset_id"],
        "owner_uid": os.getuid(),
        "policy_release": RELEASE,
        "real_participant_admission": False,
        "external_model_enabled": False,
    }


def open_store(config: dict, *, recovery: bool = False) -> ParticipantStudyStore:
    from app.cloud_deletion_authority import CloudDeletionAuthority, LambdaDeletionWriter

    root = Path(config["storage_root"])
    digests = load_participant_policy(
        root, root / "control/notice.txt", root / "control/policy.json", release=RELEASE
    )
    identity, region = instance_identity()
    authority = CloudDeletionAuthority(
        aws_client("dynamodb", region),
        config["table"],
        config["dataset_fingerprint"],
        identity,
        writer=LambdaDeletionWriter(aws_client("lambda", region), config["authority_function"]),
    )
    return ParticipantStudyStore(
        root, **digests, remote_authority=authority, _defer_recovery=recovery
    )


def _backup_directory(root: Path) -> Path:
    work = root / "cloud-backups"
    if not work.exists():
        work.mkdir(mode=0o700)
    return work


def backup(config: dict) -> dict:
    from app.cloud_backup import create_backup

    store = open_store(config)
    manifest = create_backup(
        store,
        aws_client("s3", config["region"]),
        config["bucket"],
        _backup_directory(store.root),
    )
    receipt = store.root / "control/last-backup.json"
    temporary = receipt.with_name(f".last-backup-{uuid.uuid4().hex}.tmp")
    try:
        _write_new(temporary, json.dumps(manifest.to_dict(), sort_keys=True).encode())
        os.replace(temporary, receipt)
    finally:
        temporary.unlink(missing_ok=True)
    fd = os.open(receipt.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return manifest.to_dict()


def backup_is_fresh(root: Path, fingerprint: str | None = None) -> bool:
    try:
        from app.cloud_backup import BackupManifest

        manifest = BackupManifest.from_dict(
            json.loads(_read_private(root / "control/last-backup.json"))
        )
        age = (datetime.now(timezone.utc) - _deadline(manifest.created_at)).total_seconds()
        return 0 <= age <= 15 * 60 and (
            fingerprint is None or manifest.dataset_fingerprint == fingerprint
        )
    except Exception:
        return False


def serve(config: dict) -> None:
    import uvicorn

    from app.protected_study import read_token_file
    from app.rehearsal_app import create_app
    from app.study_runtime import (
        PrivateStudyRuntime,
        StudyRuntimeUnavailable,
        private_server_log_config,
    )
    from app.study_storage import PrivateStudyStore

    root = Path(config["storage_root"])

    class CloudRuntime(PrivateStudyRuntime):
        def require_ready(self):
            super().require_ready()
            if _deadline(config["expires_at"]) <= datetime.now(timezone.utc) or not backup_is_fresh(
                root, config["dataset_fingerprint"]
            ):
                raise StudyRuntimeUnavailable("cloud_unavailable")

    runtime = CloudRuntime(PrivateStudyStore(root), participant_store=open_store(config))
    app = create_app(
        read_token_file(root / "control/token"),
        private_runtime=runtime,
        allowed_origin="http://localhost:8017",
        notice_text=_private_bytes(root, root / "control/notice.txt").decode("utf-8"),
    )
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8017,
        workers=1,
        access_log=False,
        proxy_headers=False,
        forwarded_allow_ips="",
        log_config=private_server_log_config(runtime),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("init", "identity", "serve", "backup", "restore", "inspect")
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--dataset-id")
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "identity":
            identity, region = instance_identity()
            result = {"instance_id": identity, "region": region}
        elif args.command == "init":
            if args.root is None:
                raise ValueError
            result = initialize(args.root, dataset_id=args.dataset_id)
        else:
            if args.config is None:
                raise ValueError
            config = read_config(args.config)
            if args.command == "serve":
                serve(config)
                return 0
            if args.command == "backup":
                result = backup(config)
            elif args.command == "restore":
                from app.cloud_backup import BackupManifest, restore_backup

                if args.manifest is None:
                    raise ValueError
                manifest = BackupManifest.from_dict(json.loads(_read_private(args.manifest)))
                store = open_store(config, recovery=True)
                result = restore_backup(
                    store,
                    aws_client("s3", config["region"]),
                    config["bucket"],
                    manifest,
                    _backup_directory(store.root),
                )
                if hasattr(result, "to_dict"):
                    result = result.to_dict()
                elif is_dataclass(result):
                    result = asdict(result)
            else:
                store = open_store(config)
                result = {**store.summary(), "backup_fresh": backup_is_fresh(store.root)}
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, StudyStorageError) else "cloud_operation_failed"
        print(json.dumps({"error": code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
