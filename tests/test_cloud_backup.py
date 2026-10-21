from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import uuid
from dataclasses import replace

import pytest

from app.cloud_backup import BackupManifest, create_backup, restore_backup
from app.participant_models import NOTICE_VERSION, POLICY_VERSION
from app.participant_storage import ParticipantStudyStore
from app.study_storage import StudyStorageError

_NOTICE = hashlib.sha256(b"approved rehearsal notice").hexdigest()
_POLICY = hashlib.sha256(b"approved rehearsal policy").hexdigest()
_TASK_ID = "formative-ux-repayment-01"


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.put_requests = []
        self.get_requests = []

    def put_object(self, **request):
        key = (request["Bucket"], request["Key"])
        assert key not in self.objects
        assert request["ServerSideEncryption"] == "AES256"
        assert request["IfNoneMatch"] == "*"
        body = request["Body"]
        value = body.read() if hasattr(body, "read") else body
        version_id = f"version-{len(self.objects) + 1}"
        self.objects[key] = (version_id, value)
        self.put_requests.append(request)
        return {"VersionId": version_id}

    def get_object(self, *, Bucket, Key, VersionId):
        key = (Bucket, Key)
        self.get_requests.append({"Bucket": Bucket, "Key": Key, "VersionId": VersionId})
        stored_version, value = self.objects[key]
        if VersionId != stored_version:
            raise KeyError("wrong object version")
        return {
            "VersionId": stored_version,
            "ContentLength": len(value),
            "Body": io.BytesIO(value),
        }


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "private-rehearsal"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    marker = root / ".study-storage.json"
    marker.write_text(
        json.dumps(
            {
                "schema": "youth-law-private-storage.v1",
                "owner_uid": os.getuid(),
                "real_participant_processing_enabled": False,
            }
        ),
        encoding="ascii",
    )
    os.chmod(marker, 0o600)
    return ParticipantStudyStore(root, notice_sha256=_NOTICE, policy_sha256=_POLICY)


@pytest.fixture
def workdir(tmp_path):
    path = tmp_path / "cloud-backups"
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def create_consent(store):
    request_key = f"{int(store.clock().timestamp())}:{uuid.uuid4()}"
    return store.create_consent(
        {
            "adult_confirmed": True,
            "participation_agreed": True,
            "observation_recording_agreed": True,
            "notice_version": NOTICE_VERSION,
            "policy_version": POLICY_VERSION,
        },
        task_id=_TASK_ID,
        request_key=request_key,
    )


def test_create_backup_writes_versioned_sse_object_and_private_atomic_receipt(
    store, workdir
):
    s3 = FakeS3()
    create_consent(store)

    manifest = create_backup(store, s3, "private-bucket", workdir)

    assert BackupManifest.from_dict(manifest.to_dict()) == manifest
    assert manifest.dataset_id == store.marker_data["dataset_id"]
    assert manifest.db_key.startswith(f"backups/{manifest.dataset_fingerprint}/")
    assert manifest.version_id == "version-1"
    assert len(s3.put_requests) == 2
    assert all(request["ServerSideEncryption"] == "AES256" for request in s3.put_requests)
    database_object = next(
        value
        for (_, key), (_, value) in s3.objects.items()
        if key.endswith(".sqlite3")
    )
    assert len(database_object) == manifest.size_bytes
    receipt = next(workdir.glob("*.receipt.json"))
    info = receipt.stat()
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_size <= 64 * 1024
    stored = json.loads(receipt.read_text("ascii"))
    assert stored["manifest"] == manifest.to_dict()
    assert stored["manifest_version_id"] == "version-2"


def test_restore_uses_exact_version_and_fenced_recovery_preserves_snapshot_rows(
    store, workdir
):
    s3 = FakeS3()
    first = create_consent(store)
    manifest = create_backup(store, s3, "private-bucket", workdir)
    create_consent(store)
    assert len(store.list_records()) == 2

    result = restore_backup(store, s3, "private-bucket", manifest, workdir)

    assert result.source_sha256 == manifest.db_sha256
    assert result.version_id == manifest.version_id
    assert [row["record_id"] for row in store.list_records()] == [first["record_id"]]
    assert s3.get_requests == [
        {
            "Bucket": "private-bucket",
            "Key": manifest.db_key,
            "VersionId": manifest.version_id,
        }
    ]


@pytest.mark.parametrize("change", ["wrong_hash", "wrong_dataset", "extra_field"])
def test_manifest_tampering_fails_closed_without_installing_backup(
    store, workdir, change
):
    s3 = FakeS3()
    create_consent(store)
    manifest = create_backup(store, s3, "private-bucket", workdir)
    create_consent(store)
    before_ids = {row["record_id"] for row in store.list_records()}

    if change == "wrong_hash":
        manifest = replace(manifest, db_sha256="0" * 64)
    elif change == "wrong_dataset":
        wrong_fingerprint = "f" * 64
        manifest = replace(
            manifest,
            dataset_fingerprint=wrong_fingerprint,
            db_key=manifest.db_key.replace(manifest.dataset_fingerprint, wrong_fingerprint),
        )
    else:
        bad = manifest.to_dict()
        bad["unexpected"] = "value"
        with pytest.raises(StudyStorageError) as error:
            restore_backup(store, s3, "private-bucket", bad, workdir)
        assert error.value.code == "invalid_backup_manifest"
        assert {row["record_id"] for row in store.list_records()} == before_ids
        return

    with pytest.raises(StudyStorageError):
        restore_backup(store, s3, "private-bucket", manifest, workdir)
    assert {row["record_id"] for row in store.list_records()} == before_ids


def test_malformed_and_wrong_version_responses_are_rejected(store, workdir):
    s3 = FakeS3()
    create_consent(store)
    manifest = create_backup(store, s3, "private-bucket", workdir)
    malformed = manifest.to_dict()
    malformed["size_bytes"] = True
    with pytest.raises(StudyStorageError) as error:
        BackupManifest.from_dict(malformed)
    assert error.value.code == "invalid_backup_manifest"

    s3.objects[("private-bucket", manifest.db_key)] = ("version-other", b"bad")
    with pytest.raises(StudyStorageError) as error:
        restore_backup(store, s3, "private-bucket", manifest, workdir)
    assert error.value.code == "invalid_backup_version"
