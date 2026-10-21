from __future__ import annotations

import ctypes
import errno
import json
import os
import pwd
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import study_storage
from app.study_storage import PrivateStudyStore, StudyStorageError


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


@pytest.fixture
def private_root(tmp_path, monkeypatch):
    # The host may give its parent folders inherited ACLs. Test the store's
    # fail-closed ACL contract directly in dedicated tests below.
    monkeypatch.setattr(study_storage, "_check_no_acl", lambda path: None)
    root = tmp_path / "private"
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
        encoding="utf-8",
    )
    os.chmod(marker, 0o600)
    return root


@pytest.fixture
def store(private_root):
    return PrivateStudyStore(private_root, clock=Clock())


def observation(**updates):
    value = {
        "scenario": "synthetic",
        "consent": "simulated_granted",
        "task_id": "formative-ux-repayment-01",
        "completion": "completed",
        "elapsed_seconds": 42,
    }
    value.update(updates)
    return value


def log_payload(**updates):
    value = {
        "event": "preparation_request",
        "request_id": str(uuid.uuid4()),
        "route": "/prepare",
        "status": 200,
        "duration_ms": 3.2,
    }
    value.update(updates)
    return value


@pytest.mark.parametrize(
    "acl_result,acl_errno,entry_result,entry_errno,expected",
    [
        (None, errno.ENOENT, None, 0, False),
        (1, 0, -1, errno.EINVAL, "acl_unverifiable"),
        (1, 0, 0, 0, True),
        (None, errno.EACCES, None, 0, "acl_unverifiable"),
        (1, 0, -1, errno.EIO, "acl_unverifiable"),
    ],
)
def test_native_acl_binding_contract(
    monkeypatch, acl_result, acl_errno, entry_result, entry_errno, expected
):
    class FakeAclLibrary:
        def acl_get_fd_np(self, fd, acl_type):
            ctypes.set_errno(acl_errno)
            return acl_result

        def acl_get_entry(self, acl, first_entry, entry_pointer):
            ctypes.set_errno(entry_errno)
            return entry_result

        def acl_free(self, acl):
            return 0

    monkeypatch.setattr(study_storage, "_load_mac_acl_library", lambda: FakeAclLibrary())
    if isinstance(expected, str):
        with pytest.raises(StudyStorageError) as error:
            study_storage._mac_acl_present(17)
        assert error.value.code == expected
    else:
        assert study_storage._mac_acl_present(17) is expected


def test_macos_native_acl_inspects_real_no_acl_and_extended_acl(tmp_path):
    if os.uname().sysname != "Darwin":
        pytest.skip("macOS ACL API integration")
    path = tmp_path / "acl-check.txt"
    path.write_text("synthetic only", encoding="ascii")
    os.chmod(path, 0o600)
    study_storage._check_no_acl(path)

    username = pwd.getpwuid(os.getuid()).pw_name
    added = subprocess.run(
        ["/bin/chmod", "+a", f"user:{username} allow read", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert added.returncode == 0, added.stderr
    try:
        with pytest.raises(StudyStorageError) as error:
            study_storage._check_no_acl(path)
        assert error.value.code == "acl_present"
    finally:
        subprocess.run(["/bin/chmod", "-a#", "0", str(path)], check=False)


@pytest.mark.parametrize("result", ["absent", "present", "unverifiable"])
def test_posix_xattr_acl_checks_fail_closed(monkeypatch, tmp_path, result):
    monkeypatch.setattr(study_storage.os, "uname", lambda: type("Host", (), {"sysname": "Linux"}))
    path = tmp_path / "acl-check"
    path.write_text("synthetic only", encoding="ascii")

    def getxattr(target, attr, *, follow_symlinks):
        if result == "present":
            return b"acl"
        if result == "unverifiable":
            raise OSError(errno.EACCES, "denied")
        raise OSError(getattr(errno, "ENODATA", errno.ENOENT), "absent")

    monkeypatch.setattr(study_storage.os, "getxattr", getxattr, raising=False)
    if result == "absent":
        study_storage._check_no_acl(path)
    else:
        with pytest.raises(StudyStorageError) as error:
            study_storage._check_no_acl(path)
        assert error.value.code == ("acl_present" if result == "present" else "acl_unverifiable")


def test_observation_idempotency_preserves_original_deadline_and_conflicts(store):
    record_id = str(uuid.uuid4())
    first = store.write_observation(observation(), record_id=record_id)
    store.clock.value += timedelta(days=2)
    duplicate = store.write_observation(observation(), record_id=record_id)

    assert duplicate == first
    assert duplicate["expires_at"] == "2026-11-19T12:00:00.000000Z"
    with pytest.raises(StudyStorageError) as error:
        store.write_observation(observation(elapsed_seconds=43), record_id=record_id)
    assert error.value.code == "conflict"


def test_exact_ttl_boundaries_and_expired_read_denial(store):
    observation_record = store.write_observation(observation())
    store.write_log(log_payload())
    store.clock.value += timedelta(hours=24)
    assert store.summary() == {
        "observations": 1,
        "logs": 0,
        "observations_expired": 0,
        "logs_expired": 1,
    }
    assert store.purge_expired() == {"observations_deleted": 0, "logs_deleted": 1}
    assert store.read_observation(observation_record["id"]) == observation_record
    store.clock.value += timedelta(days=30) - timedelta(hours=24)
    assert store.read_observation(observation_record["id"]) is None
    assert store.purge_expired() == {"observations_deleted": 1, "logs_deleted": 0}


def test_restart_catches_up_expiry_and_removes_only_own_temp_files(private_root):
    clock = Clock()
    first = PrivateStudyStore(private_root, clock=clock)
    first.write_observation(observation())
    runtime = private_root / "app-runtime-v1"
    temp = runtime / "observations" / f".tmp-{uuid.uuid4()}"
    temp.write_text("partial", encoding="ascii")
    os.chmod(temp, 0o600)
    clock.value += timedelta(days=31)

    restarted = PrivateStudyStore(private_root, clock=clock)
    assert not temp.exists()
    assert restarted.purge_expired() == {"observations_deleted": 1, "logs_deleted": 0}


def test_startup_recovers_real_publication_when_temp_unlink_fails(private_root, monkeypatch):
    clock = Clock()
    first = PrivateStudyStore(private_root, clock=clock)
    record_id = str(uuid.uuid4())
    real_unlink = Path.unlink

    def fail_temp_unlink(path, *args, **kwargs):
        if path.name == f".tmp-{record_id}":
            raise OSError("simulated stop after publication")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_temp_unlink)
    with pytest.raises(StudyStorageError) as error:
        first.write_observation(observation(), record_id=record_id)
    assert error.value.code == "filesystem_error"
    monkeypatch.setattr(Path, "unlink", real_unlink)
    final = first.observations / f"{record_id}.json"
    temp = first.observations / f".tmp-{record_id}"
    assert final.exists() and temp.exists()
    assert os.stat(final).st_ino == os.stat(temp).st_ino

    restarted = PrivateStudyStore(private_root, clock=clock)
    assert not temp.exists()
    assert restarted.read_observation(record_id)["id"] == record_id


def test_partial_unlink_failure_keeps_retryable_expired_record(store, monkeypatch):
    record = store.write_observation(observation())
    store.clock.value += timedelta(days=30)
    real_unlink = Path.unlink
    failed = False

    def fail_once(path, *args, **kwargs):
        nonlocal failed
        if path.name == f"{record['id']}.json" and not failed:
            failed = True
            raise OSError("simulated unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_once)
    with pytest.raises(StudyStorageError) as error:
        store.purge_expired()
    assert error.value.code == "filesystem_error"
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert store.read_observation(record["id"]) is None
    assert store.purge_expired() == {"observations_deleted": 1, "logs_deleted": 0}


def test_concurrent_writes_and_purges_are_serialized(store):
    def write(index):
        payload = observation(elapsed_seconds=index)
        store.write_observation(payload)
        store.purge_expired()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(50)))
    assert store.summary() == {
        "observations": 50,
        "logs": 0,
        "observations_expired": 0,
        "logs_expired": 0,
    }


@pytest.mark.parametrize(
    "payload",
    [
        observation(note="free text"),
        observation(consent="real_participant_granted"),
        observation(scenario="real"),
        observation(elapsed_seconds=True),
        observation(task_id="other"),
        observation(task_id=[]),
        observation(completion={}),
    ],
)
def test_observations_reject_extra_or_real_participant_fields(store, payload):
    with pytest.raises(StudyStorageError) as error:
        store.write_observation(payload)
    assert error.value.code == "invalid_record"


@pytest.mark.parametrize(
    "payload",
    [
        log_payload(body="request content"),
        log_payload(route="/prepare?email=secret"),
        log_payload(exception_class="PermissionError"),
        log_payload(event=[]),
        log_payload(route={}),
        log_payload(exception_class=[]),
        log_payload(duration_ms=float("nan")),
        log_payload(duration_ms=10**10000),
        log_payload(status=True),
    ],
)
def test_logs_reject_unallowlisted_or_nonfinite_metadata(store, payload):
    with pytest.raises(StudyStorageError) as error:
        store.write_log(payload)
    assert error.value.code == "invalid_record"


def test_symlinked_runtime_directory_is_rejected(private_root, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    os.chmod(target, 0o700)
    (private_root / "app-runtime-v1").symlink_to(target, target_is_directory=True)
    with pytest.raises(StudyStorageError) as error:
        PrivateStudyStore(private_root)
    assert error.value.code in {"unsafe_path", "unsafe_mode"}


def test_hardlinked_record_is_rejected(store, tmp_path):
    record = store.write_observation(observation())
    source = store.observations / f"{record['id']}.json"
    os.link(source, tmp_path / "alias.json")
    with pytest.raises(StudyStorageError) as error:
        store.read_observation(record["id"])
    assert error.value.code == "unsafe_file"


def test_wrong_mode_and_unknown_files_fail_closed(store):
    record = store.write_observation(observation())
    path = store.observations / f"{record['id']}.json"
    os.chmod(path, 0o640)
    with pytest.raises(StudyStorageError) as mode_error:
        store.read_observation(record["id"])
    assert mode_error.value.code == "unsafe_file"
    os.chmod(path, 0o600)
    (store.logs / "unexpected.txt").write_text("x", encoding="ascii")
    with pytest.raises(StudyStorageError) as unknown_error:
        store.summary()
    assert unknown_error.value.code == "unexpected_file"


def test_acl_inspection_failure_fails_closed(private_root, monkeypatch):
    def denied(path):
        raise StudyStorageError("acl_unverifiable")

    monkeypatch.setattr(study_storage, "_check_no_acl", denied)
    with pytest.raises(StudyStorageError) as error:
        PrivateStudyStore(private_root)
    assert error.value.code == "acl_unverifiable"


def test_root_marker_is_revalidated_before_locking(store):
    marker = store.root / ".study-storage.json"
    marker.write_text("{}", encoding="ascii")
    os.chmod(marker, 0o600)
    with pytest.raises(StudyStorageError) as error:
        store.summary()
    assert error.value.code == "invalid_marker"


def test_unexpected_runtime_level_entry_fails_closed(store):
    unexpected = store.runtime / "retained-copy.json"
    unexpected.write_text("{}", encoding="ascii")
    os.chmod(unexpected, 0o600)
    with pytest.raises(StudyStorageError) as error:
        store.summary()
    assert error.value.code == "unexpected_file"


def test_delete_observation_is_idempotent(store):
    record = store.write_observation(observation())
    assert store.delete_observation(record["id"]) is True
    assert store.delete_observation(record["id"]) is False


def test_expired_idempotent_write_does_not_revive_or_extend(store):
    record_id = str(uuid.uuid4())
    original = store.write_observation(observation(), record_id=record_id)
    store.clock.value += timedelta(days=30)
    with pytest.raises(StudyStorageError) as error:
        store.write_observation(observation(), record_id=record_id)
    assert error.value.code == "expired"
    assert store.read_observation(record_id) is None
    assert (store.observations / f"{record_id}.json").exists()
    assert original["expires_at"] == "2026-11-19T12:00:00.000000Z"


def test_empty_idempotency_key_is_rejected(store):
    with pytest.raises(StudyStorageError) as error:
        store.write_observation(observation(), record_id="")
    assert error.value.code == "invalid_record"


def test_log_capacity_is_independent_and_normal_write_acl_checks_are_constant(tmp_path):
    assert study_storage.CAPACITY_LIMITS == {"observations": 2000, "logs": 10000}
    assert PrivateStudyStore.CAPACITY_LIMITS == study_storage.CAPACITY_LIMITS
    if os.uname().sysname != "Darwin":
        pytest.skip("native ACL call-count regression is macOS-specific")

    root = tmp_path / "private-scale"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    (root / ".study-storage.json").write_text(
        json.dumps(
            {
                "schema": "youth-law-private-storage.v1",
                "owner_uid": os.getuid(),
                "real_participant_processing_enabled": False,
            }
        ),
        encoding="ascii",
    )
    os.chmod(root / ".study-storage.json", 0o600)
    store = PrivateStudyStore(root, clock=Clock())
    assert store.capacity_limits == {"observations": 2000, "logs": 10000}
    created = "2026-10-20T12:00:00.000000Z"
    expires = "2026-10-21T12:00:00.000000Z"
    payload = log_payload()
    for number in range(3000):
        record_id = str(uuid.UUID(int=number + 1, version=4))
        record = {
            "schema": "youth-law-study-record.v1",
            "kind": "log",
            "id": record_id,
            "created_at": created,
            "expires_at": expires,
            "payload": payload,
        }
        path = store.logs / f"{record_id}.json"
        path.write_bytes(study_storage._json_bytes(record))
        os.chmod(path, 0o600)

    native_checks = 0
    original_acl_check = study_storage._mac_acl_present

    def counted_acl_check(fd):
        nonlocal native_checks
        native_checks += 1
        return original_acl_check(fd)

    started = time.perf_counter()
    study_storage._mac_acl_present = counted_acl_check
    try:
        store.write_log(log_payload())
    finally:
        study_storage._mac_acl_present = original_acl_check
    elapsed = time.perf_counter() - started
    assert native_checks <= 12
    assert len(list(store.logs.iterdir())) == 3001
    print(f"native ACL calls for 3001-log write: {native_checks}; elapsed={elapsed:.3f}s")


def test_full_log_capacity_fails_explicitly_without_dropping_records(store, monkeypatch):
    monkeypatch.setattr(study_storage, "LOG_CAPACITY", 3)
    for _ in range(3):
        store.write_log(log_payload())
    with pytest.raises(StudyStorageError) as error:
        store.write_log(log_payload())
    assert error.value.code == "capacity"
    assert len(list(store.logs.glob("*.json"))) == 3
    assert store.summary()["logs"] == 3
