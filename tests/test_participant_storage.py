from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app import participant_storage
from app.participant_models import NOTICE_VERSION, POLICY_VERSION
from app.participant_storage import ParticipantStudyStore
from app.study_storage import StudyStorageError

_TASK_ID = "formative-ux-repayment-01"
_NOTICE_DIGEST = hashlib.sha256(b"approved notice fixture").hexdigest()
_POLICY_DIGEST = hashlib.sha256(b"approved policy fixture").hexdigest()


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.value


@pytest.fixture
def rehearsal_root(tmp_path):
    # Use a clearly labelled, isolated fixture root; never touch operator data.
    root = tmp_path / "rehearsals" / f"participant-tests-{uuid.uuid4()}"
    root.mkdir(mode=0o700, parents=True)
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
    yield root


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(rehearsal_root, clock):
    return ParticipantStudyStore(
        rehearsal_root,
        notice_sha256=_NOTICE_DIGEST,
        policy_sha256=_POLICY_DIGEST,
        clock=clock,
    )


def consent(**updates):
    value = {
        "adult_confirmed": True,
        "participation_agreed": True,
        "observation_recording_agreed": True,
        "notice_version": NOTICE_VERSION,
        "policy_version": POLICY_VERSION,
    }
    value.update(updates)
    return value


def observation(**updates):
    value = {
        "task_status": "completed",
        "elapsed_seconds_monotonic": 45,
        "help_requested_during_task": "no",
        "content_help_during_task": "no",
        "technical_issue_during_task": "no",
        "stuck_event_count_during_task": 0,
        "next_action_comprehension_post_task": "observed_understood",
        "source_link_date_location_comprehension_post_task": "observed_understood",
        "preparation_list_meaning_comprehension_post_task": "observed_understood",
        "card_limits_comprehension_post_task": "observed_understood",
        "critical_misunderstanding_observed": "no_observed",
        "critical_misunderstanding_phase": "unknown",
        "sensitive_data_handling_status": "not_observed",
        "recording_status": "not_recorded",
    }
    value.update(updates)
    return value


def request_key(clock: Clock, *, offset_seconds: int = 0) -> str:
    timestamp = int(clock.value.timestamp()) + offset_seconds
    return f"{timestamp}:{uuid.uuid4()}"


def create(store, clock, *, key=None, consent_value=None):
    return store.create_consent(
        consent_value or consent(),
        task_id=_TASK_ID,
        request_key=key or request_key(clock),
    )


def test_create_and_idempotent_replay_store_only_hash_and_server_metadata(store, clock):
    key = request_key(clock)
    first = create(store, clock, key=key)
    replay = create(store, clock, key=key)

    assert first == replay
    assert first["record_origin"] == "developer_rehearsal"
    assert first["notice_sha256"] == _NOTICE_DIGEST
    assert first["policy_sha256"] == _POLICY_DIGEST
    assert first["observation_provenance"] == "operator_reported_rehearsal_state"
    assert first["elapsed_seconds_provenance"] == "operator_observed"
    created = datetime.fromisoformat(first["created_at"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(first["expires_at"].replace("Z", "+00:00"))
    assert expires - created == timedelta(days=30)
    raw = store.database.read_bytes()
    assert key.encode() not in raw
    assert hashlib.sha256(key.encode()).hexdigest().encode() in raw
    assert ParticipantStudyStore.snapshot()["storage_row_ceiling"] == 1000


@pytest.mark.parametrize(
    "bad_consent",
    [
        {"adult_confirmed": 1},
        {"extra": "field"},
        {"notice_version": "unapproved"},
    ],
)
def test_invalid_consent_schema_fails_closed(store, clock, bad_consent):
    value = consent()
    value.update(bad_consent)
    with pytest.raises(StudyStorageError) as error:
        create(store, clock, consent_value=value)
    assert error.value.code == "invalid_record"
    assert store.summary()["developer_rehearsal_records"] == 0


def test_refusal_creates_no_record_or_id_and_does_not_need_request_key(store, clock):
    assert (
        store.create_consent(
            consent(participation_agreed=False), task_id=_TASK_ID, request_key=None
        )
        is None
    )
    assert (
        store.create_consent(
            consent(observation_recording_agreed=False), task_id=_TASK_ID, request_key=None
        )
        is None
    )
    assert store.summary()["developer_rehearsal_records"] == 0


def test_real_participant_origin_is_closed_and_task_ids_are_allowlisted(store, clock):
    with pytest.raises(StudyStorageError) as error:
        store.create_consent(
            consent(),
            task_id=_TASK_ID,
            request_key=request_key(clock),
            record_origin="participant",
        )
    assert error.value.code == "admission_closed"
    with pytest.raises(StudyStorageError):
        store.create_consent(consent(), task_id="other", request_key=request_key(clock))


def test_request_key_timestamp_has_canonical_bounded_length(store, clock):
    oversized = f"{int(clock.value.timestamp())}0:{uuid.uuid4()}"
    with pytest.raises(StudyStorageError) as error:
        create(store, clock, key=oversized)
    assert error.value.code == "invalid_request_key"


@pytest.mark.parametrize(
    "bad_observation",
    [
        {"extra_text": "free text is forbidden"},
        {"elapsed_seconds_monotonic": True},
        {"stuck_event_count_during_task": 101},
        {"task_status": "timeout", "elapsed_seconds_monotonic": 359},
        {"critical_misunderstanding_observed": "yes"},
        {"task_status": "not_run"},
    ],
)
def test_observation_schema_and_cross_field_errors(store, clock, bad_observation):
    record = create(store, clock)
    with pytest.raises(StudyStorageError) as error:
        store.write_observation(
            record["record_id"], observation(**bad_observation), expected_version=1
        )
    assert error.value.code == "invalid_record"


def test_valid_not_run_observation_requires_null_metrics_and_not_observed_values(store, clock):
    record = create(store, clock)
    value = observation(
        task_status="not_run",
        elapsed_seconds_monotonic=None,
        stuck_event_count_during_task=None,
        help_requested_during_task="not_observed",
        content_help_during_task="not_observed",
        technical_issue_during_task="not_observed",
        next_action_comprehension_post_task="not_observed",
        source_link_date_location_comprehension_post_task="not_observed",
        preparation_list_meaning_comprehension_post_task="not_observed",
        card_limits_comprehension_post_task="not_observed",
        critical_misunderstanding_observed="not_observed",
    )
    updated = store.write_observation(record["record_id"], value, expected_version=1)
    assert updated["version"] == 2
    assert updated["observation"] == value


def test_observation_update_is_idempotent_and_uses_optimistic_version(store, clock):
    record = create(store, clock)
    first = store.write_observation(record["record_id"], observation(), expected_version=1)
    replay = store.write_observation(record["record_id"], observation(), expected_version=1)
    assert first["version"] == replay["version"] == 2
    with pytest.raises(StudyStorageError) as error:
        store.write_observation(
            record["record_id"], observation(task_status="partial"), expected_version=1
        )
    assert error.value.code == "conflict"


def test_withdrawal_blocks_replay_after_restart_until_original_expiry(rehearsal_root, clock):
    store = ParticipantStudyStore(
        rehearsal_root,
        notice_sha256=_NOTICE_DIGEST,
        policy_sha256=_POLICY_DIGEST,
        clock=clock,
    )
    key = request_key(clock)
    record = create(store, clock, key=key)
    assert store.withdraw(record["record_id"]) is True
    assert store.withdraw(record["record_id"]) is False
    restarted = ParticipantStudyStore(
        rehearsal_root,
        notice_sha256=_NOTICE_DIGEST,
        policy_sha256=_POLICY_DIGEST,
        clock=clock,
    )
    with pytest.raises(StudyStorageError) as error:
        create(restarted, clock, key=key)
    assert error.value.code == "revoked"
    assert restarted.summary()["revocations"] == 1
    clock.value += timedelta(days=30)
    assert restarted.purge_expired() == {
        "participant_records_deleted": 0,
        "participant_revocations_deleted": 1,
    }
    with pytest.raises(StudyStorageError) as error:
        create(restarted, clock, key=key)
    assert error.value.code == "invalid_request_key"


def test_expired_record_reads_as_none_and_rejects_update_while_summary_counts_it(store, clock):
    record = create(store, clock)
    clock.value += timedelta(days=30)
    assert store.read_record(record["record_id"]) is None
    assert store.summary()["expired_records"] == 1
    with pytest.raises(StudyStorageError) as error:
        store.write_observation(record["record_id"], observation(), expected_version=1)
    assert error.value.code == "expired"
    assert store.purge_expired()["participant_records_deleted"] == 1


def test_rollback_does_not_leave_partial_record(store, clock, monkeypatch):
    with store._locked():
        with store._connect_locked() as connection:
            connection.execute(
                """CREATE TRIGGER injected_insert_failure BEFORE INSERT ON records
                BEGIN SELECT RAISE(ABORT, 'injected rollback case'); END"""
            )
    monkeypatch.setattr(store, "_audit_locked", lambda connection: 0)
    with pytest.raises(StudyStorageError) as error:
        create(store, clock)
    assert error.value.code == "database_error"
    monkeypatch.undo()
    with sqlite3.connect(store.database) as connection:
        connection.execute("DROP TRIGGER injected_insert_failure")
    assert store.summary()["developer_rehearsal_records"] == 0


def test_concurrent_compare_and_swap_allows_only_one_changed_write(store, clock):
    record = create(store, clock)
    barrier = threading.Barrier(2)

    def update(status):
        barrier.wait()
        try:
            result = store.write_observation(
                record["record_id"], observation(task_status=status), expected_version=1
            )
            return ("ok", result["version"])
        except StudyStorageError as exc:
            return (exc.code, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["completed", "partial"]))
    assert sorted(result[0] for result in results) == ["conflict", "ok"]
    assert store.read_record(record["record_id"])["version"] == 2


def test_uncommitted_withdrawal_recovers_after_process_exit(rehearsal_root, store, clock):
    record = create(store, clock)
    updated = store.write_observation(record["record_id"], observation(), expected_version=1)
    child = """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('PRAGMA synchronous=FULL')
connection.execute('PRAGMA secure_delete=ON')
connection.execute('PRAGMA cache_size=1')
connection.execute('BEGIN IMMEDIATE')
connection.execute('INSERT INTO revocations SELECT request_key_hash,expires_at FROM records')
connection.execute('DELETE FROM records')
os._exit(17)
"""
    result = subprocess.run([sys.executable, "-c", child, str(store.database)], timeout=5)
    assert result.returncode == 17
    assert store.database.with_name(store.database.name + "-journal").exists()
    restarted = ParticipantStudyStore(
        rehearsal_root,
        notice_sha256=_NOTICE_DIGEST,
        policy_sha256=_POLICY_DIGEST,
        clock=clock,
    )
    assert restarted.read_record(record["record_id"]) == updated
    assert restarted.summary()["revocations"] == 0
    assert restarted.withdraw(record["record_id"]) is True
    assert restarted.read_record(record["record_id"]) is None
    assert restarted.summary()["revocations"] == 1


def test_rejects_foreign_root_without_creating_participant_runtime(tmp_path, clock):
    foreign = tmp_path / "foreign"
    foreign.mkdir(mode=0o700)
    os.chmod(foreign, 0o700)
    with pytest.raises(StudyStorageError):
        ParticipantStudyStore(
            foreign,
            notice_sha256=_NOTICE_DIGEST,
            policy_sha256=_POLICY_DIGEST,
            clock=clock,
        )
    assert not (foreign / "participant-runtime-v1").exists()


def test_unmarked_existing_database_is_not_adopted(rehearsal_root, clock):
    runtime = rehearsal_root / "participant-runtime-v1"
    runtime.mkdir(mode=0o700)
    os.chmod(runtime, 0o700)
    database = runtime / "participant.sqlite3"
    database.write_bytes(b"unmarked database bytes")
    os.chmod(database, 0o600)
    with pytest.raises(StudyStorageError) as error:
        ParticipantStudyStore(
            rehearsal_root,
            notice_sha256=_NOTICE_DIGEST,
            policy_sha256=_POLICY_DIGEST,
            clock=clock,
        )
    assert error.value.code == "unsafe_marker"
    assert not (runtime / ".participant-storage.json").exists()
    assert database.read_bytes() == b"unmarked database bytes"


def test_missing_or_truncated_managed_database_is_not_recreated(rehearsal_root, clock):
    options = {
        "notice_sha256": _NOTICE_DIGEST,
        "policy_sha256": _POLICY_DIGEST,
        "clock": clock,
    }
    store = ParticipantStudyStore(rehearsal_root, **options)
    store.database.unlink()
    with pytest.raises(StudyStorageError):
        ParticipantStudyStore(rehearsal_root, **options)
    assert not store.database.exists()

    # Restore a fresh namespace for the truncation case, then leave an empty DB.
    runtime = store.runtime
    for child in runtime.iterdir():
        child.unlink()
    runtime.rmdir()
    # This is an explicit synthetic namespace reset, so remove its sibling
    # deletion ledger too; normal startup must fail closed if only one is lost.
    import shutil

    shutil.rmtree(rehearsal_root / "participant-deletions-v1")
    store = ParticipantStudyStore(rehearsal_root, **options)
    store.database.write_bytes(b"")
    os.chmod(store.database, 0o600)
    with pytest.raises(StudyStorageError):
        ParticipantStudyStore(rehearsal_root, **options)
    assert store.database.read_bytes() == b""


def test_rejects_changed_digest_pin_and_insecure_runtime_files(rehearsal_root, clock):
    ParticipantStudyStore(
        rehearsal_root,
        notice_sha256=_NOTICE_DIGEST,
        policy_sha256=_POLICY_DIGEST,
        clock=clock,
    )
    with pytest.raises(StudyStorageError) as error:
        ParticipantStudyStore(
            rehearsal_root,
            notice_sha256="a" * 64,
            policy_sha256=_POLICY_DIGEST,
            clock=clock,
        )
    assert error.value.code == "invalid_marker"


def test_unexpected_database_sidecar_fails_closed(store):
    sidecar = store.runtime / f"{store.database.name}-wal"
    sidecar.write_bytes(b"unexpected")
    os.chmod(sidecar, 0o600)
    with pytest.raises(StudyStorageError) as error:
        store.summary()
    assert error.value.code == "unexpected_file"


@pytest.mark.parametrize(
    "corrupt_sql,corrupt_values",
    [
        (
            "UPDATE records SET notice_sha256=? WHERE record_id=?",
            ("f" * 64,),
        ),
        (
            "UPDATE records SET consent_json=? WHERE record_id=?",
            ("consent",),
        ),
        (
            "UPDATE records SET request_key_hash=? WHERE record_id=?",
            ("not-a-hash",),
        ),
        (
            "UPDATE records SET version=2 WHERE record_id=?",
            (),
        ),
    ],
)
def test_full_audit_rejects_persisted_record_corruption(store, clock, corrupt_sql, corrupt_values):
    record = create(store, clock)
    if "consent_json" in corrupt_sql:
        corrupt_values = (json.dumps(consent(participation_agreed=False)),)
    sql_parameters = (*corrupt_values, record["record_id"])
    with sqlite3.connect(store.database) as connection:
        connection.execute(corrupt_sql, sql_parameters)

    with pytest.raises(StudyStorageError) as error:
        store.summary()
    assert error.value.code == "invalid_store"


@pytest.mark.parametrize("operation", ["create", "purge", "summary"])
def test_full_audit_precedes_create_purge_and_summary(store, clock, operation):
    record = create(store, clock)
    with sqlite3.connect(store.database) as connection:
        connection.execute(
            "UPDATE records SET notice_sha256=? WHERE record_id=?",
            ("f" * 64, record["record_id"]),
        )

    with pytest.raises(StudyStorageError) as error:
        if operation == "create":
            create(store, clock)
        elif operation == "purge":
            store.purge_expired()
        else:
            store.summary()
    assert error.value.code == "invalid_store"


def test_startup_full_audit_rejects_persisted_corruption(rehearsal_root, store, clock):
    record = create(store, clock)
    with sqlite3.connect(store.database) as connection:
        connection.execute(
            "UPDATE records SET policy_sha256=? WHERE record_id=?",
            ("a" * 64, record["record_id"]),
        )

    with pytest.raises(StudyStorageError) as error:
        ParticipantStudyStore(
            rehearsal_root,
            notice_sha256=_NOTICE_DIGEST,
            policy_sha256=_POLICY_DIGEST,
            clock=clock,
        )
    assert error.value.code == "invalid_store"


def test_full_audit_rejects_noncanonical_revocation_before_idempotency_lookup(store, clock):
    key = request_key(clock)
    record = create(store, clock, key=key)
    assert store.withdraw(record["record_id"]) is True
    with sqlite3.connect(store.database) as connection:
        connection.execute("UPDATE revocations SET request_key_hash=?", ("not-a-hash",))

    with pytest.raises(StudyStorageError) as error:
        create(store, clock, key=key)
    assert error.value.code == "invalid_store"


def test_full_audit_rejects_noncanonical_revocation_expiry(store, clock):
    record = create(store, clock)
    store.withdraw(record["record_id"])
    with sqlite3.connect(store.database) as connection:
        connection.execute("UPDATE revocations SET expires_at='2026-11-19T12:00:00+00:00'")

    with pytest.raises(StudyStorageError) as error:
        store.summary()
    assert error.value.code == "invalid_store"


@pytest.mark.parametrize("operation", ["read", "list"])
def test_read_paths_audit_corrupted_revocation_ledger(store, clock, operation):
    record = create(store, clock)
    assert store.withdraw(record["record_id"])
    with sqlite3.connect(store.database) as connection:
        connection.execute("UPDATE revocations SET request_key_hash='broken'")

    with pytest.raises(StudyStorageError) as error:
        if operation == "read":
            store.read_record(record["record_id"])
        else:
            store.list_records()
    assert error.value.code == "invalid_store"


def test_list_records_returns_only_minimal_unexpired_rows_newest_first(store, clock):
    first = create(store, clock)
    clock.value += timedelta(seconds=1)
    second = create(store, clock)
    clock.value += timedelta(seconds=1)
    third = create(store, clock)
    updated = store.write_observation(third["record_id"], observation(), expected_version=1)

    rows = store.list_records()

    assert [row["record_id"] for row in rows] == [
        third["record_id"],
        second["record_id"],
        first["record_id"],
    ]
    assert rows[0] == {
        "record_id": updated["record_id"],
        "task_id": _TASK_ID,
        "expires_at": updated["expires_at"],
        "version": 2,
    }
    assert all(set(row) == {"record_id", "task_id", "expires_at", "version"} for row in rows)
    assert store.list_records(limit=1) == rows[:1]
    assert store.list_records(limit=20) == rows

    clock.value += timedelta(days=31)
    assert store.list_records() == []


@pytest.mark.parametrize("limit", [0, 21, True, False, 1.5, "2", None])
def test_list_records_rejects_non_integer_or_out_of_range_limit(store, limit):
    with pytest.raises(StudyStorageError) as error:
        store.list_records(limit=limit)
    assert error.value.code == "invalid_record"


def test_full_audit_rejects_inconsistent_observation_version(store, clock):
    record = create(store, clock)
    store.write_observation(record["record_id"], observation(), expected_version=1)
    with sqlite3.connect(store.database) as connection:
        connection.execute("UPDATE records SET version=1 WHERE record_id=?", (record["record_id"],))

    with pytest.raises(StudyStorageError) as error:
        store.purge_expired()
    assert error.value.code == "invalid_store"


def test_storage_row_ceiling_counts_records_and_revocations(store, clock, monkeypatch):
    monkeypatch.setattr(participant_storage, "_STORAGE_ROW_CEILING", 2)
    key_one = request_key(clock)
    record_one = create(store, clock, key=key_one)
    assert store.withdraw(record_one["record_id"]) is True

    key_two = request_key(clock)
    record_two = create(store, clock, key=key_two)
    # Existing idempotent writes are returned before capacity is enforced.
    assert create(store, clock, key=key_two) == record_two

    with pytest.raises(StudyStorageError) as error:
        create(store, clock, key=request_key(clock))
    assert error.value.code == "capacity"
    assert store.summary()["revocations"] == 1
    assert store.summary()["developer_rehearsal_records"] == 1


def test_corrupt_database_fails_closed(store):
    store.database.write_bytes(b"not a sqlite database")
    os.chmod(store.database, 0o600)
    with pytest.raises(StudyStorageError):
        store.summary()


def test_runtime_files_are_owner_only_and_native_acl_guard_is_active(store):
    for path in (store.runtime, store.database, store.marker, store.lock_path):
        assert path.stat().st_uid == os.getuid()
        assert (path.stat().st_mode & 0o777) == (0o700 if path.is_dir() else 0o600)
    # Constructor and this read exercise the platform ACL inspector directly.
    assert store.summary()["participant_records"] == 0
