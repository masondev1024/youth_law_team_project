"""Private, explicit loading of participant notice and policy artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import stat

import pytest

from app import participant_policy
from app.participant_models import NOTICE_VERSION, POLICY_VERSION
from app.participant_policy import load_participant_policy
from app.study_storage import StudyStorageError


@pytest.fixture
def private_policy_files(tmp_path, monkeypatch):
    root = tmp_path / "private-study"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    control = root / "control"
    control.mkdir(mode=0o700)
    os.chmod(control, 0o700)

    notice_bytes = (
        "Finance formative usability study notice\n"
        f"Version: {NOTICE_VERSION}\n"
        "Scope: local developer rehearsal only.\n"
    ).encode("utf-8")
    policy_bytes = json.dumps(
        {
            "versions": {"notice": NOTICE_VERSION, "policy": POLICY_VERSION},
            "scope_and_admission": {
                "origin": "developer_rehearsal",
                "real_participant_admission": False,
                "recruitment_authorized": False,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    notice_file = control / "participant-notice.md"
    policy_file = control / "owner-decisions.json"
    notice_file.write_bytes(notice_bytes)
    policy_file.write_bytes(policy_bytes)
    os.chmod(notice_file, 0o600)
    os.chmod(policy_file, 0o600)
    monkeypatch.setattr(
        participant_policy, "REHEARSAL_NOTICE_SHA256", hashlib.sha256(notice_bytes).hexdigest()
    )
    monkeypatch.setattr(
        participant_policy, "REHEARSAL_POLICY_SHA256", hashlib.sha256(policy_bytes).hexdigest()
    )
    return {
        "root": root,
        "control": control,
        "notice_file": notice_file,
        "policy_file": policy_file,
        "notice_bytes": notice_bytes,
        "policy_bytes": policy_bytes,
    }


def write_policy(
    files,
    *,
    notice_version=NOTICE_VERSION,
    policy_version=POLICY_VERSION,
    origin="developer_rehearsal",
    real_admission=False,
    recruitment=False,
):
    files["policy_bytes"] = json.dumps(
        {
            "versions": {"notice": notice_version, "policy": policy_version},
            "scope_and_admission": {
                "origin": origin,
                "real_participant_admission": real_admission,
                "recruitment_authorized": recruitment,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    files["policy_file"].write_bytes(files["policy_bytes"])
    os.chmod(files["policy_file"], 0o600)


def test_loader_hashes_the_exact_explicit_immutable_artifact_bytes(private_policy_files):
    files = private_policy_files
    result = load_participant_policy(files["root"], files["notice_file"], files["policy_file"])

    assert result == {
        "notice_sha256": hashlib.sha256(files["notice_bytes"]).hexdigest(),
        "policy_sha256": hashlib.sha256(files["policy_bytes"]).hexdigest(),
    }
    assert result["notice_sha256"] == hashlib.sha256(files["notice_file"].read_bytes()).hexdigest()
    assert result["policy_sha256"] == hashlib.sha256(files["policy_file"].read_bytes()).hexdigest()
    assert set(result) == {"notice_sha256", "policy_sha256"}


@pytest.mark.parametrize(
    "policy_changes",
    [
        {"real_admission": True},
        {"recruitment": True},
        {"origin": "participant"},
        {"notice_version": "old-notice"},
        {"policy_version": "old-policy"},
    ],
)
def test_loader_fails_closed_on_open_admission_or_version_drift(
    private_policy_files, policy_changes
):
    files = private_policy_files
    write_policy(files, **policy_changes)

    with pytest.raises((ValueError, StudyStorageError)):
        load_participant_policy(files["root"], files["notice_file"], files["policy_file"])


def test_loader_rejects_notice_without_the_pinned_version(private_policy_files, monkeypatch):
    files = private_policy_files
    files["notice_file"].write_bytes(b"Finance study notice without a version")
    os.chmod(files["notice_file"], 0o600)
    # Even a content hash pinned to a malformed test release cannot bypass the
    # independent notice-version check.
    monkeypatch.setattr(
        participant_policy,
        "REHEARSAL_NOTICE_SHA256",
        hashlib.sha256(files["notice_file"].read_bytes()).hexdigest(),
    )

    with pytest.raises(ValueError, match="unrecognized notice version"):
        load_participant_policy(files["root"], files["notice_file"], files["policy_file"])


@pytest.mark.parametrize("unsafe_mode", [0o755, 0o640])
def test_loader_rejects_public_or_group_accessible_control_files(private_policy_files, unsafe_mode):
    files = private_policy_files
    target = files["control"] if unsafe_mode == 0o755 else files["notice_file"]
    os.chmod(target, unsafe_mode)

    with pytest.raises((ValueError, StudyStorageError)):
        load_participant_policy(files["root"], files["notice_file"], files["policy_file"])


def test_loader_rejects_symlinked_notice_and_hardlinked_policy(private_policy_files):
    files = private_policy_files
    symlink = files["control"] / "notice-link.md"
    symlink.symlink_to(files["notice_file"])
    with pytest.raises((ValueError, StudyStorageError)):
        load_participant_policy(files["root"], symlink, files["policy_file"])

    linked_policy = files["control"] / "policy-link.json"
    os.link(files["policy_file"], linked_policy)
    with pytest.raises((ValueError, StudyStorageError)):
        load_participant_policy(files["root"], files["notice_file"], linked_policy)
    assert stat.S_ISREG(files["policy_file"].lstat().st_mode)


def test_loader_rejects_files_outside_the_control_directory(private_policy_files, tmp_path):
    files = private_policy_files
    outside = tmp_path / "outside-policy.json"
    outside.write_bytes(files["policy_bytes"])
    os.chmod(outside, 0o600)

    with pytest.raises(ValueError, match="invalid private policy path"):
        load_participant_policy(files["root"], files["notice_file"], outside)


def test_loader_uses_explicit_paths_and_ignores_environment_path_hints(
    private_policy_files, monkeypatch, tmp_path
):
    files = private_policy_files
    misleading = tmp_path / "environment-policy.json"
    misleading.write_text("{}", encoding="utf-8")
    os.chmod(misleading, 0o600)
    monkeypatch.setenv("PARTICIPANT_NOTICE_FILE", str(misleading))
    monkeypatch.setenv("PARTICIPANT_POLICY_FILE", str(misleading))

    result = load_participant_policy(files["root"], files["notice_file"], files["policy_file"])

    assert result["notice_sha256"] == hashlib.sha256(files["notice_bytes"]).hexdigest()
    assert result["policy_sha256"] == hashlib.sha256(files["policy_bytes"]).hexdigest()


@pytest.mark.parametrize("changed_file", ["notice_file", "policy_file"])
def test_loader_rejects_changed_content_even_when_version_labels_remain(
    private_policy_files, changed_file
):
    files = private_policy_files
    target = files[changed_file]
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="unrecognized rehearsal release content"):
        load_participant_policy(files["root"], files["notice_file"], files["policy_file"])
