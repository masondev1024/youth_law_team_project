from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from app.cloud_policy import DIGESTS, NOTICE, POLICY, RELEASE
from app.cloud_rehearsal import aws_client, initialize, read_config
from app.participant_policy import load_participant_policy
from app.study_storage import StudyStorageError


def test_cloud_release_is_explicit_and_preserves_local_policy_pins(tmp_path):
    root = tmp_path / "synthetic"
    result = initialize(root)
    assert result["real_participant_admission"] is False
    assert result["external_model_enabled"] is False
    assert (root / "control/notice.txt").read_bytes() == NOTICE
    assert (root / "control/policy.json").read_bytes() == POLICY
    args = (root, root / "control/notice.txt", root / "control/policy.json")
    with pytest.raises(ValueError, match="unrecognized"):
        load_participant_policy(*args)
    assert tuple(load_participant_policy(*args, release=RELEASE).values()) == DIGESTS
    (root / "control/policy.json").write_bytes(POLICY + b" ")
    with pytest.raises(ValueError, match="unrecognized"):
        load_participant_policy(*args, release=RELEASE)


def test_initialize_never_replaces_existing_store_or_token(tmp_path):
    root = tmp_path / "synthetic"
    initialize(root)
    token = (root / "control/token").read_bytes()
    with pytest.raises(StudyStorageError, match="fresh_root_required"):
        initialize(root)
    assert (root / "control/token").read_bytes() == token


def _config(tmp_path):
    return {
        "schema": "youth-law-aws-rehearsal.v1",
        "run_id": "ylaw-20261021-test",
        "region": "ap-northeast-2",
        "storage_root": str(tmp_path / "synthetic"),
        "table": "ylaw-rehearsal",
        "bucket": "ylaw-rehearsal-test",
        "dataset_fingerprint": "a" * 64,
        "port": 8017,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "log_group": "/youthlaw/rehearsal-test",
        "authority_function": "ylaw-rehearsal-writer",
    }


def _save(tmp_path, value):
    path = tmp_path / "cloud.json"
    path.write_text(json.dumps(value))
    os.chmod(path, 0o600)
    return path


def test_config_rejects_expired_run_and_extra_enable_flags(tmp_path):
    value = _config(tmp_path)
    path = _save(tmp_path, value)
    assert read_config(path) == value
    value["real_participant_admission"] = True
    with pytest.raises(StudyStorageError, match="invalid_cloud_config"):
        read_config(_save(tmp_path, value))
    value.pop("real_participant_admission")
    value["expires_at"] = "2020-01-01T00:00:00Z"
    with pytest.raises(StudyStorageError, match="run_expired"):
        read_config(_save(tmp_path, value))


def test_config_rejects_shared_permissions_or_symlink(tmp_path):
    path = _save(tmp_path, _config(tmp_path))
    os.chmod(path, 0o644)
    with pytest.raises(StudyStorageError, match="unsafe_cloud_config"):
        read_config(path)
    os.chmod(path, 0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(StudyStorageError):
        read_config(link)


def test_synchronous_writer_deadline_covers_lambda_and_does_not_blind_retry(monkeypatch):
    import boto3

    captured = {}

    class Session:
        def client(self, service, **kwargs):
            captured[service] = kwargs["config"]
            return object()

    monkeypatch.setattr(boto3, "Session", lambda **_kwargs: Session())
    aws_client("lambda", "ap-northeast-2")
    aws_client("dynamodb", "ap-northeast-2")
    assert captured["lambda"].read_timeout > 10  # Deployed handler's maximum duration.
    assert captured["lambda"].retries["total_max_attempts"] == 1
    assert captured["dynamodb"].retries["total_max_attempts"] == 1
