from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import app.participant_deletion_ledger as ledger_module
import app.participant_recovery as recovery_module
from app.participant_models import NOTICE_VERSION, POLICY_VERSION
from app.participant_recovery import bootstrap_ledger, restore_database
from app.participant_storage import ParticipantStudyStore
from app.study_storage import StudyStorageError

_NOTICE = hashlib.sha256(b"test notice").hexdigest()
_POLICY = hashlib.sha256(b"test policy").hexdigest()
_TASK = "formative-ux-repayment-01"


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


@pytest.fixture
def private_root(tmp_path):
    root = tmp_path / "participant-store"
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
    return root


@pytest.fixture
def clock():
    return Clock()


def make_store(root, clock, **kwargs):
    configuration = {
        "notice_sha256": _NOTICE,
        "policy_sha256": _POLICY,
        "clock": clock,
    }
    configuration.update(kwargs)
    return ParticipantStudyStore(
        root,
        **configuration,
    )


def make_consent():
    return {
        "adult_confirmed": True,
        "participation_agreed": True,
        "observation_recording_agreed": True,
        "notice_version": NOTICE_VERSION,
        "policy_version": POLICY_VERSION,
    }


def key_for(clock):
    return f"{int(clock.value.timestamp())}:{uuid.uuid4()}"


def make_record(store, clock, request_key=None):
    return store.create_consent(
        make_consent(),
        task_id=_TASK,
        request_key=request_key or key_for(clock),
    )


def save_backup(store, path):
    shutil.copyfile(store.database, path)
    os.chmod(path, 0o600)
    return path.read_bytes()


def restore(store, backup):
    return restore_database(
        store,
        backup,
        expected_ledger_checkpoint=store.ledger.checkpoint_id(),
    )


def test_old_backup_restore_replays_latest_withdrawal_before_reads(private_root, clock, tmp_path):
    store = make_store(private_root, clock)
    key = key_for(clock)
    record = make_record(store, clock, key)
    retained = make_record(store, clock)
    backup = tmp_path / "pre-withdrawal.sqlite3"
    backup_bytes = save_backup(store, backup)

    assert store.withdraw(record["record_id"])
    result = restore(store, backup)

    assert result["records_withdrawn"] == 1
    assert backup.read_bytes() == backup_bytes
    assert store.read_record(record["record_id"]) is None
    assert store.read_record(retained["record_id"]) is not None
    with pytest.raises(StudyStorageError) as error:
        make_record(store, clock, key)
    assert error.value.code == "revoked"


def test_startup_replays_intent_committed_before_sqlite_delete(private_root, clock):
    store = make_store(private_root, clock)
    key = key_for(clock)
    record = make_record(store, clock, key)
    with store._locked(), store._connect_locked() as connection:
        row = connection.execute(
            "SELECT request_key_hash,expires_at FROM records WHERE record_id=?",
            (record["record_id"],),
        ).fetchone()
        store.ledger.append(row["request_key_hash"], row["expires_at"], clock.value)

    restarted = make_store(private_root, clock)
    assert restarted.read_record(record["record_id"]) is None
    assert restarted.summary()["revocations"] == 1
    with pytest.raises(StudyStorageError) as error:
        make_record(restarted, clock, key)
    assert error.value.code == "revoked"


def test_startup_recovers_complete_anchored_journal_suffix_after_checkpoint_failure(
    private_root, clock, monkeypatch
):
    store = make_store(private_root, clock)
    key = key_for(clock)
    record = make_record(store, clock, key)

    def fail_checkpoint(*_args, **_kwargs):
        raise StudyStorageError("injected_checkpoint_failure")

    monkeypatch.setattr(store.ledger, "_write_checkpoint", fail_checkpoint)
    with pytest.raises(StudyStorageError):
        store.withdraw(record["record_id"])
    temporary = store.ledger.path / ".checkpoint.tmp"
    temporary.write_text("partial", encoding="ascii")
    os.chmod(temporary, 0o600)

    restarted = make_store(private_root, clock)
    assert restarted.ledger.checkpoint_id().startswith("1:")
    assert restarted.read_record(record["record_id"]) is None
    with pytest.raises(StudyStorageError) as error:
        make_record(restarted, clock, key)
    assert error.value.code == "revoked"


def test_short_journal_write_never_reports_withdrawal_success_or_recovers_partial_tail(
    private_root, clock, monkeypatch
):
    store = make_store(private_root, clock)
    record = make_record(store, clock)
    original_write = os.write
    calls = 0

    def partial_then_fail(fd, value):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original_write(fd, value[:1])
        raise OSError("injected disk full")

    monkeypatch.setattr(ledger_module.os, "write", partial_then_fail)
    with pytest.raises(StudyStorageError):
        store.withdraw(record["record_id"])
    with sqlite3.connect(store.database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_id=?",
            (record["record_id"],),
        ).fetchone()[0] == 1
    partial = store.ledger.journal.read_bytes()
    assert partial and not partial.endswith(b"\n")

    monkeypatch.setattr(ledger_module.os, "write", original_write)
    with pytest.raises(StudyStorageError):
        make_store(private_root, clock)


def test_checkpoint_only_rollback_is_detected_by_external_highwater(
    private_root, clock
):
    store = make_store(private_root, clock)
    old_checkpoint = store.ledger.checkpoint.read_bytes()
    key = key_for(clock)
    record = make_record(store, clock, key)
    with store._locked(), store._connect_locked() as connection:
        row = connection.execute(
            "SELECT request_key_hash,expires_at FROM records WHERE record_id=?",
            (record["record_id"],),
        ).fetchone()
        store.ledger.append(row["request_key_hash"], row["expires_at"], clock.value)
    store.ledger.checkpoint.write_bytes(old_checkpoint)
    os.chmod(store.ledger.checkpoint, 0o600)
    with pytest.raises(StudyStorageError) as error:
        store.ledger.verified_entries()
    assert error.value.code == "stale_checkpoint"


def test_restore_fails_closed_for_missing_stale_corrupt_or_wrong_dataset_ledger(
    private_root, clock, tmp_path
):
    store = make_store(private_root, clock)
    record = make_record(store, clock)
    backup = tmp_path / "backup.sqlite3"
    save_backup(store, backup)
    checkpoint = store.ledger.checkpoint_id()

    with pytest.raises(StudyStorageError):
        restore_database(store, backup, expected_ledger_checkpoint="1:" + "0" * 64)
    assert store.read_record(record["record_id"]) is not None

    checkpoint_path = store.ledger.checkpoint
    checkpoint_path.write_text("{}", encoding="ascii")
    os.chmod(checkpoint_path, 0o600)
    database_before = hashlib.sha256(store.database.read_bytes()).hexdigest()
    with pytest.raises(StudyStorageError):
        restore_database(store, backup, expected_ledger_checkpoint=checkpoint)
    assert hashlib.sha256(store.database.read_bytes()).hexdigest() == database_before


def test_missing_ledger_on_existing_namespace_is_never_recreated(private_root, clock):
    store = make_store(private_root, clock)
    shutil.rmtree(private_root / "participant-deletions-v1")
    with pytest.raises(StudyStorageError):
        make_store(private_root, clock)
    assert store.database.exists()


def test_same_policy_foreign_database_is_rejected_by_full_dataset_uuid(
    private_root, clock, tmp_path
):
    first = make_store(private_root, clock)
    make_record(first, clock)
    backup = tmp_path / "foreign.sqlite3"
    save_backup(first, backup)

    other_root = tmp_path / "other-store"
    other_root.mkdir(mode=0o700)
    os.chmod(other_root, 0o700)
    (other_root / ".study-storage.json").write_text(
        json.dumps(
            {
                "schema": "youth-law-private-storage.v1",
                "owner_uid": os.getuid(),
                "real_participant_processing_enabled": False,
            }
        ),
        encoding="ascii",
    )
    os.chmod(other_root / ".study-storage.json", 0o600)
    second = make_store(other_root, clock)
    with pytest.raises(StudyStorageError) as error:
        restore(second, backup)
    assert error.value.code == "wrong_dataset"
    assert second.summary()["developer_rehearsal_records"] == 0


def test_explicit_v1_bootstrap_checks_expected_digest_before_migration(private_root, clock):
    store = make_store(private_root, clock)
    key = key_for(clock)
    record = make_record(store, clock, key)
    assert store.withdraw(record["record_id"])
    with sqlite3.connect(store.database) as connection:
        connection.execute("DROP TABLE dataset_binding")
        connection.execute("PRAGMA user_version=1")
    legacy_marker = {
        "schema": "youth-law-participant-records.v1",
        "schema_version": 1,
        "owner_uid": os.getuid(),
        "notice_sha256": _NOTICE,
        "policy_sha256": _POLICY,
        "record_origin": "developer_rehearsal",
        "participant_admission_enabled": False,
    }
    store.marker.write_text(json.dumps(legacy_marker), encoding="ascii")
    os.chmod(store.marker, 0o600)
    shutil.rmtree(private_root / "participant-deletions-v1")
    store.service_lock_path.unlink()
    expected = hashlib.sha256(store.database.read_bytes()).hexdigest()
    marker_before = store.marker.read_bytes()

    legacy_store = make_store(private_root, clock, _allow_ledger_bootstrap=True)
    with pytest.raises(StudyStorageError):
        bootstrap_ledger(legacy_store, expected_db_sha256="0" * 64)
    assert store.database.with_name(".participant-service.lock").exists() is False
    assert store.marker.read_bytes() == marker_before

    with pytest.raises(StudyStorageError) as error:
        bootstrap_ledger(legacy_store, expected_db_sha256=expected)
    assert error.value.code == "legacy_services_must_be_stopped"
    result = bootstrap_ledger(
        legacy_store,
        expected_db_sha256=expected,
        confirm_legacy_services_stopped=True,
    )
    assert result["active_revocations_seeded"] == 1
    migrated = make_store(private_root, clock)
    with pytest.raises(StudyStorageError) as error:
        make_record(migrated, clock, key)
    assert error.value.code == "revoked"


def test_v1_bootstrap_reuses_database_uuid_after_marker_publication_failure(
    private_root, clock, monkeypatch
):
    store = make_store(private_root, clock)
    key = key_for(clock)
    record = make_record(store, clock, key)
    assert store.withdraw(record["record_id"])
    with sqlite3.connect(store.database) as connection:
        connection.execute("DROP TABLE dataset_binding")
        connection.execute("PRAGMA user_version=1")
    legacy_marker = {
        "schema": "youth-law-participant-records.v1",
        "schema_version": 1,
        "owner_uid": os.getuid(),
        "notice_sha256": _NOTICE,
        "policy_sha256": _POLICY,
        "record_origin": "developer_rehearsal",
        "participant_admission_enabled": False,
    }
    store.marker.write_text(json.dumps(legacy_marker), encoding="ascii")
    os.chmod(store.marker, 0o600)
    shutil.rmtree(private_root / "participant-deletions-v1")
    store.service_lock_path.unlink()

    legacy_store = make_store(private_root, clock, _allow_ledger_bootstrap=True)
    original_write_marker = recovery_module._write_marker

    def fail_marker_publication(_store):
        raise StudyStorageError("injected_marker_failure")

    monkeypatch.setattr(recovery_module, "_write_marker", fail_marker_publication)
    with pytest.raises(StudyStorageError):
        bootstrap_ledger(
            legacy_store,
            expected_db_sha256=hashlib.sha256(store.database.read_bytes()).hexdigest(),
            confirm_legacy_services_stopped=True,
        )
    with sqlite3.connect(store.database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        first_dataset_id = connection.execute(
            "SELECT dataset_id FROM dataset_binding"
        ).fetchone()[0]
    assert json.loads(store.marker.read_text(encoding="ascii")) == legacy_marker

    monkeypatch.setattr(recovery_module, "_write_marker", original_write_marker)
    retry_store = make_store(private_root, clock, _allow_ledger_bootstrap=True)
    result = bootstrap_ledger(
        retry_store,
        expected_db_sha256=hashlib.sha256(store.database.read_bytes()).hexdigest(),
        confirm_legacy_services_stopped=True,
    )
    assert result["active_revocations_seeded"] == 1
    assert json.loads(store.marker.read_text(encoding="ascii"))["dataset_id"] == first_dataset_id
    migrated = make_store(private_root, clock)
    with pytest.raises(StudyStorageError) as error:
        make_record(migrated, clock, key)
    assert error.value.code == "revoked"


def test_restore_checks_dataset_and_backup_permissions_without_touching_source(
    private_root, clock, tmp_path
):
    store = make_store(private_root, clock)
    record = make_record(store, clock)
    backup = tmp_path / "backup.sqlite3"
    original = save_backup(store, backup)
    with pytest.raises(StudyStorageError):
        make_store(private_root, clock, notice_sha256=hashlib.sha256(b"other").hexdigest())
    os.chmod(backup, 0o644)
    with pytest.raises(StudyStorageError):
        restore(store, backup)
    assert store.read_record(record["record_id"]) is not None
    os.chmod(backup, 0o600)
    assert backup.read_bytes() == original


def test_restore_is_repeatable_and_blocks_while_service_lease_is_held(
    private_root, clock, tmp_path
):
    store = make_store(private_root, clock)
    record = make_record(store, clock)
    backup = tmp_path / "backup.sqlite3"
    save_backup(store, backup)
    assert store.withdraw(record["record_id"])
    with store.service_lease():
        with pytest.raises(StudyStorageError) as error:
            restore(store, backup)
        assert error.value.code == "service_active"
    assert restore(store, backup)["records_withdrawn"] == 1
    assert restore(store, backup)["records_withdrawn"] == 1
    assert store.read_record(record["record_id"]) is None


def test_two_service_shared_leases_coexist_and_fence_restore(private_root, clock, tmp_path):
    first = make_store(private_root, clock)
    second = make_store(private_root, clock)
    backup = tmp_path / "backup.sqlite3"
    save_backup(first, backup)
    with first.service_lease(shared=True), second.service_lease(shared=True):
        with pytest.raises(StudyStorageError) as error:
            restore(first, backup)
        assert error.value.code == "service_active"


def test_checkpoint_read_waits_for_participant_lock(private_root, clock):
    store = make_store(private_root, clock)
    attempted = threading.Event()
    result = []

    def read_checkpoint():
        attempted.set()
        result.append(store.current_ledger_checkpoint())

    with store._locked():
        worker = threading.Thread(target=read_checkpoint)
        worker.start()
        assert attempted.wait(timeout=1)
        time.sleep(0.05)
        assert result == []
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert result == [store.ledger.checkpoint_id()]


def test_deferred_recovery_validation_does_not_write_before_exclusive_fence(
    private_root, clock, tmp_path
):
    store = make_store(private_root, clock)
    key = key_for(clock)
    record = make_record(store, clock, key)
    backup = tmp_path / "pre-withdrawal.sqlite3"
    save_backup(store, backup)
    with store._locked(), store._connect_locked() as connection:
        row = connection.execute(
            "SELECT request_key_hash,expires_at FROM records WHERE record_id=?",
            (record["record_id"],),
        ).fetchone()
        store.ledger.append(row["request_key_hash"], row["expires_at"], clock.value)
    database_before = hashlib.sha256(store.database.read_bytes()).hexdigest()

    deferred = make_store(private_root, clock, _defer_recovery=True)
    assert hashlib.sha256(store.database.read_bytes()).hexdigest() == database_before
    with store.service_lease(shared=True):
        with pytest.raises(StudyStorageError) as error:
            restore_database(
                deferred,
                backup,
                expected_ledger_checkpoint=deferred.ledger.checkpoint_id(),
            )
        assert error.value.code == "service_active"
    assert hashlib.sha256(store.database.read_bytes()).hexdigest() == database_before

    with pytest.raises(StudyStorageError):
        restore_database(deferred, backup, expected_ledger_checkpoint="wrong")
    assert hashlib.sha256(store.database.read_bytes()).hexdigest() == database_before


def test_expired_ledger_entries_compact_without_reopening_withdrawn_record(
    private_root, clock
):
    store = make_store(private_root, clock)
    key = key_for(clock)
    record = make_record(store, clock, key)
    assert store.withdraw(record["record_id"])
    clock.value += timedelta(days=30)
    store.purge_expired()
    entries, checkpoint = store.ledger.verified_entries()
    assert entries == []
    assert checkpoint["sequence"] == 1
    assert store.summary()["revocations"] == 0


def test_compaction_recovers_after_journal_replace_before_checkpoint(
    private_root, clock, monkeypatch
):
    store = make_store(private_root, clock)
    first_key = key_for(clock)
    first_record = make_record(store, clock, first_key)
    assert store.withdraw(first_record["record_id"])
    clock.value += timedelta(days=20)
    second_key = key_for(clock)
    second_record = make_record(store, clock, second_key)
    assert store.withdraw(second_record["record_id"])
    clock.value += timedelta(days=11)

    def fail_checkpoint(*_args, **_kwargs):
        raise StudyStorageError("injected_compaction_checkpoint_failure")

    monkeypatch.setattr(store.ledger, "_write_checkpoint", fail_checkpoint)
    with pytest.raises(StudyStorageError):
        store.purge_expired()
    assert (store.ledger.path / ".compaction.json").exists()

    restarted = make_store(private_root, clock)
    entries, checkpoint = restarted.ledger.verified_entries()
    assert [entry["request_key_hash"] for entry in entries] == [
        hashlib.sha256(second_key.encode("ascii")).hexdigest()
    ]
    assert checkpoint["sequence"] == 3
    assert restarted.read_record(first_record["record_id"]) is None
    assert restarted.read_record(second_record["record_id"]) is None


def test_compaction_aborts_safely_when_journal_replace_never_happened(
    private_root, clock, monkeypatch
):
    store = make_store(private_root, clock)
    first_record = make_record(store, clock)
    assert store.withdraw(first_record["record_id"])
    clock.value += timedelta(days=20)
    second_record = make_record(store, clock)
    assert store.withdraw(second_record["record_id"])
    clock.value += timedelta(days=11)
    original_replace = os.replace

    def fail_journal_replace(source, target):
        if target == store.ledger.journal:
            raise OSError("injected journal replacement failure")
        return original_replace(source, target)

    monkeypatch.setattr(os, "replace", fail_journal_replace)
    with pytest.raises(StudyStorageError):
        store.ledger.compact(clock.value)
    assert (store.ledger.path / ".compaction.json").exists()

    monkeypatch.setattr(os, "replace", original_replace)
    restarted = make_store(private_root, clock)
    entries, checkpoint = restarted.ledger.verified_entries()
    assert len(entries) == 2
    assert checkpoint["sequence"] == 2
    assert not (restarted.ledger.path / ".compaction.json").exists()
    restarted.ledger.compact(clock.value)
    compacted, checkpoint = restarted.ledger.verified_entries()
    assert len(compacted) == 1
    assert checkpoint["sequence"] == 3


def test_compaction_finishes_after_checkpoint_publish_before_highwater(
    private_root, clock, monkeypatch
):
    store = make_store(private_root, clock)
    first_record = make_record(store, clock)
    assert store.withdraw(first_record["record_id"])
    clock.value += timedelta(days=20)
    second_record = make_record(store, clock)
    assert store.withdraw(second_record["record_id"])
    clock.value += timedelta(days=11)

    def fail_highwater(*_args, **_kwargs):
        raise StudyStorageError("injected_highwater_failure")

    monkeypatch.setattr(store.ledger, "_write_highwater", fail_highwater)
    with pytest.raises(StudyStorageError):
        store.ledger.compact(clock.value)
    assert (store.ledger.path / ".compaction.json").exists()

    restarted = make_store(private_root, clock)
    entries, checkpoint = restarted.ledger.verified_entries()
    assert len(entries) == 1
    assert checkpoint["sequence"] == 3
    assert not (restarted.ledger.path / ".compaction.json").exists()


def test_compaction_cleans_manifest_after_all_checkpoint_files_are_published(
    private_root, clock, monkeypatch
):
    store = make_store(private_root, clock)
    first_record = make_record(store, clock)
    assert store.withdraw(first_record["record_id"])
    clock.value += timedelta(days=20)
    second_record = make_record(store, clock)
    assert store.withdraw(second_record["record_id"])
    clock.value += timedelta(days=11)
    manifest = store.ledger.path / ".compaction.json"
    original_unlink = type(manifest).unlink

    def fail_manifest_unlink(path, *args, **kwargs):
        if path == manifest:
            raise OSError("injected manifest cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(manifest), "unlink", fail_manifest_unlink)
    with pytest.raises(StudyStorageError):
        store.ledger.compact(clock.value)
    assert manifest.exists()
    checkpoint_before = store.ledger.checkpoint.read_bytes()
    highwater_before = (store.ledger.path / "highwater.json").read_bytes()

    monkeypatch.setattr(type(manifest), "unlink", original_unlink)
    restarted = make_store(private_root, clock)
    entries, checkpoint = restarted.ledger.verified_entries()
    assert len(entries) == 1
    assert checkpoint["sequence"] == 3
    assert restarted.ledger.checkpoint.read_bytes() == checkpoint_before
    assert (restarted.ledger.path / "highwater.json").read_bytes() == highwater_before
    assert not manifest.exists()
