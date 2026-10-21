from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.participant_deletion_ledger import ParticipantDeletionLedger
from app.participant_models import NOTICE_VERSION, POLICY_VERSION
from app.participant_recovery import restore_database
from app.participant_storage import ParticipantStudyStore
from app.rehearsal_app import create_app
from app.study_runtime import PrivateStudyRuntime
from app.study_storage import PrivateStudyStore, StudyStorageError

_NOTICE = hashlib.sha256(b"cloud integration notice").hexdigest()
_POLICY = hashlib.sha256(b"cloud integration policy").hexdigest()
_TASK = "formative-ux-repayment-01"


class FakeCloudAuthority:
    """Deterministic authority fake with the same contract as the AWS adapter."""

    def __init__(self, dataset_fingerprint: str):
        self.dataset_fingerprint = dataset_fingerprint
        self.intents: dict[str, tuple[str, int]] = {}
        self.highwater = 0
        self.failure: str | None = None
        self.owner_current = True
        self.unknown_after_commit = False

    def assert_current(self) -> int:
        self._check()
        return 1

    def append(self, request_key_hash: str, expires_at: str) -> int:
        self._check()
        existing = self.intents.get(request_key_hash)
        if existing:
            if existing[0] != expires_at:
                raise StudyStorageError("intent_conflict")
            return existing[1]
        self.highwater += 1
        self.intents[request_key_hash] = (expires_at, self.highwater)
        if self.unknown_after_commit:
            self.unknown_after_commit = False
            raise StudyStorageError("authority_unavailable")
        return self.highwater

    def snapshot(self):
        self._check()
        from app.cloud_deletion_authority import DeletionIntent

        intents = [
            DeletionIntent(request_hash, expires_at, sequence)
            for request_hash, (expires_at, sequence) in self.intents.items()
        ]
        return sorted(intents, key=lambda item: item.sequence), self.highwater

    def _check(self) -> None:
        if self.failure:
            raise StudyStorageError(self.failure)
        if not self.owner_current:
            raise StudyStorageError("stale_epoch")


class Clock:
    def __call__(self):
        return datetime(2026, 10, 21, 2, tzinfo=timezone.utc)


@pytest.fixture
def private_root(tmp_path: Path) -> Path:
    root = tmp_path / "private-study"
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
    return root


def make_store(root: Path, **kwargs) -> ParticipantStudyStore:
    return ParticipantStudyStore(
        root,
        notice_sha256=_NOTICE,
        policy_sha256=_POLICY,
        clock=Clock(),
        **kwargs,
    )


def consent() -> dict:
    return {
        "adult_confirmed": True,
        "participation_agreed": True,
        "observation_recording_agreed": True,
        "notice_version": NOTICE_VERSION,
        "policy_version": POLICY_VERSION,
    }


def cloud_store(root: Path) -> tuple[ParticipantStudyStore, FakeCloudAuthority]:
    base = make_store(root)
    fingerprint = ParticipantDeletionLedger.fingerprint(base.marker_data)
    authority = FakeCloudAuthority(fingerprint)
    return make_store(root, remote_authority=authority), authority


def test_remote_commit_after_crash_is_applied_before_old_backup_install(private_root):
    store, authority = cloud_store(private_root)
    request_key = f"{int(Clock()().timestamp())}:c0a80124-1234-4234-8234-123456789abc"
    record = store.create_consent(consent(), task_id=_TASK, request_key=request_key)
    assert record is not None

    backup = private_root / "old-backup.sqlite3"
    shutil.copyfile(store.database, backup)
    os.chmod(backup, 0o600)
    original_backup_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
    connection = sqlite3.connect(store.database)
    try:
        request_hash, expires_at = connection.execute(
            "SELECT request_key_hash, expires_at FROM records WHERE record_id=?",
            (record["record_id"],),
        ).fetchone()
    finally:
        connection.close()

    # Model the durable DDB commit followed by process death before local I/O.
    authority.append(request_hash, expires_at)
    recovery_store = make_store(
        private_root, remote_authority=authority, _defer_recovery=True
    )
    expected_checkpoint = recovery_store.current_ledger_checkpoint()
    outcome = restore_database(
        recovery_store,
        backup,
        expected_ledger_checkpoint=expected_checkpoint,
    )

    assert outcome["records_withdrawn"] == 1
    assert outcome["source_sha256"] == original_backup_hash
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == original_backup_hash
    assert recovery_store.read_record(record["record_id"]) is None
    assert authority.highwater == 1


def test_old_disk_with_valid_empty_journal_reconciles_before_store_is_exposed(private_root):
    store, authority = cloud_store(private_root)
    request_key = f"{int(Clock()().timestamp())}:c0a80124-1234-4234-8234-123456789abf"
    record = store.create_consent(consent(), task_id=_TASK, request_key=request_key)
    assert record is not None
    old_database = private_root / "old-empty-ledger.sqlite3"
    shutil.copyfile(store.database, old_database)
    os.chmod(old_database, 0o600)
    old_ledger = private_root / "old-ledger"
    shutil.copytree(private_root / "participant-deletions-v1", old_ledger)

    # Remote commit succeeds, but the machine is then restored to a disk image
    # from before local journal and SQLite deletion were persisted.
    assert store.withdraw(record["record_id"])
    store.database.write_bytes(old_database.read_bytes())
    os.chmod(store.database, 0o600)
    shutil.rmtree(private_root / "participant-deletions-v1")
    shutil.copytree(old_ledger, private_root / "participant-deletions-v1")

    recovered = make_store(private_root, remote_authority=authority)
    assert recovered.read_record(record["record_id"]) is None
    assert recovered.summary()["revocations"] == 1


def test_ambiguous_remote_commit_is_not_acknowledged_and_replays_safely(private_root):
    store, authority = cloud_store(private_root)
    request_key = f"{int(Clock()().timestamp())}:c0a80124-1234-4234-8234-123456789ac0"
    record = store.create_consent(consent(), task_id=_TASK, request_key=request_key)
    assert record is not None
    authority.unknown_after_commit = True

    with pytest.raises(StudyStorageError) as error:
        store.withdraw(record["record_id"])
    assert error.value.code == "authority_unavailable"
    assert authority.highwater == 1
    connection = sqlite3.connect(store.database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_id=?", (record["record_id"],)
        ).fetchone()[0] == 1
    finally:
        connection.close()

    replayed = make_store(private_root, remote_authority=authority)
    assert replayed.read_record(record["record_id"]) is None


def test_sigkill_after_remote_commit_recovers_cold_journal_before_cloud_startup(
    private_root,
):
    store = make_store(private_root)
    request_key = f"{int(Clock()().timestamp())}:c0a80124-1234-4234-8234-123456789ac1"
    record = store.create_consent(consent(), task_id=_TASK, request_key=request_key)
    assert record is not None
    fingerprint = ParticipantDeletionLedger.fingerprint(store.marker_data)
    remote_state = private_root.parent / f"{private_root.name}-remote.json"
    source = r'''
import json, os, signal, sys
from datetime import datetime, timezone
from pathlib import Path
from app.participant_storage import ParticipantStudyStore

root, notice, policy, fingerprint, record_id, remote_path = sys.argv[1:]
remote_path = Path(remote_path)
class Clock:
    def __call__(self):
        return datetime(2026, 10, 21, 2, tzinfo=timezone.utc)
class Authority:
    dataset_fingerprint = fingerprint
    def assert_current(self):
        return 1
    def snapshot(self):
        if not remote_path.exists():
            return [], 0
        value = json.loads(remote_path.read_text())
        from app.cloud_deletion_authority import DeletionIntent
        return [DeletionIntent(value["hash"], value["expiry"], 1)], 1
    def append(self, request_hash, expires_at):
        fd = os.open(remote_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            json.dump({"hash": request_hash, "expiry": expires_at}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.kill(os.getpid(), signal.SIGKILL)
store = ParticipantStudyStore(
    Path(root), notice_sha256=notice, policy_sha256=policy,
    clock=Clock(), remote_authority=Authority(),
)
store.withdraw(record_id)
'''
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            source,
            str(private_root),
            _NOTICE,
            _POLICY,
            fingerprint,
            record["record_id"],
            str(remote_state),
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == -signal.SIGKILL
    assert remote_state.exists()

    database = Path(store.database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_id=?", (record["record_id"],)
        ).fetchone()[0] == 1
    journal = Path(f"{database}-journal")
    journal_details = {
        "exists": journal.exists(),
        "size": journal.stat().st_size if journal.exists() else None,
        "header": journal.read_bytes()[:16].hex() if journal.exists() else None,
    }
    assert journal_details["exists"] is True
    assert journal_details["header"].startswith("0000000000000000")

    authority = FakeCloudAuthority(fingerprint)
    remote_intent = json.loads(remote_state.read_text(encoding="ascii"))
    authority.intents[remote_intent["hash"]] = (remote_intent["expiry"], 1)
    authority.highwater = 1
    try:
        recovered = make_store(private_root, remote_authority=authority)
    except StudyStorageError as exc:
        pytest.fail(
            f"cloud startup failed after killed withdrawal: {exc.code}; journal={journal_details}"
        )
    assert recovered.read_record(record["record_id"]) is None
    assert not journal.exists()


def test_nonzero_unrecognized_rollback_journal_fails_closed(private_root):
    store = make_store(private_root)
    journal = Path(f"{store.database}-journal")
    journal.write_bytes(b"not-a-sqlite-journal".ljust(600, b"!"))
    os.chmod(journal, 0o600)

    with pytest.raises(StudyStorageError) as error:
        make_store(private_root)
    assert error.value.code == "invalid_journal"
    assert journal.exists()


def test_initial_dataset_id_is_allowed_only_for_a_new_uuid_bound_namespace(private_root):
    dataset_id = "d9428888-122b-4c33-9a00-8a77a40f2e7d"
    store = make_store(private_root, _initial_dataset_id=dataset_id)
    assert store.marker_data["dataset_id"] == dataset_id

    with pytest.raises(StudyStorageError) as error:
        make_store(private_root, _initial_dataset_id=dataset_id)
    assert error.value.code == "invalid_configuration"


@pytest.mark.parametrize(
    ("failure", "owner_current", "expected_code"),
    [
        ("authority_unavailable", True, "authority_unavailable"),
        (None, False, "stale_epoch"),
    ],
)
def test_stale_owner_or_authority_outage_fails_closed_without_local_data_loss(
    private_root, failure, owner_current, expected_code
):
    existing = make_store(private_root)
    request_key = f"{int(Clock()().timestamp())}:c0a80124-1234-4234-8234-123456789abd"
    record = existing.create_consent(consent(), task_id=_TASK, request_key=request_key)
    assert record is not None
    fingerprint = ParticipantDeletionLedger.fingerprint(existing.marker_data)
    authority = FakeCloudAuthority(fingerprint)
    authority.failure = failure
    authority.owner_current = owner_current

    with pytest.raises(StudyStorageError) as error:
        make_store(private_root, remote_authority=authority)
    assert error.value.code == expected_code

    # Failed cloud readiness/reconciliation must not mutate the preserved DB.
    check = sqlite3.connect(existing.database)
    try:
        assert check.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
    finally:
        check.close()


def test_bad_fingerprint_fails_closed_and_local_mode_remains_available(private_root):
    local = make_store(private_root)
    fingerprint = ParticipantDeletionLedger.fingerprint(local.marker_data)
    wrong_authority = FakeCloudAuthority("0" * 64 if fingerprint != "0" * 64 else "1" * 64)
    with pytest.raises(StudyStorageError) as error:
        make_store(private_root, remote_authority=wrong_authority)
    assert error.value.code == "wrong_dataset"

    # The default None keeps the existing single-host journal behavior intact.
    request_key = f"{int(Clock()().timestamp())}:c0a80124-1234-4234-8234-123456789abe"
    record = local.create_consent(consent(), task_id=_TASK, request_key=request_key)
    assert record is not None
    assert local.withdraw(record["record_id"]) is True
    assert local.read_record(record["record_id"]) is None


def test_cloud_readiness_checks_authority_and_keeps_loopback_host_boundary(private_root):
    base = make_store(private_root)
    fingerprint = ParticipantDeletionLedger.fingerprint(base.marker_data)
    authority = FakeCloudAuthority(fingerprint)
    participant_store = make_store(private_root, remote_authority=authority)
    notice_text = b"cloud integration notice".decode("ascii")
    runtime = PrivateStudyRuntime(
        PrivateStudyStore(private_root, clock=Clock()),
        participant_store=participant_store,
        interval_seconds=0.03,
    )
    app = create_app(
        "integration-test-token-never-use-outside-the-fixture",
        private_runtime=runtime,
        allowed_origin="http://localhost:8015",
        notice_text=notice_text,
    )

    with TestClient(app, base_url="http://localhost:8015", client=("127.0.0.1", 50000)) as client:
        assert client.get("/ready").status_code == 200
        authority.owner_current = False
        unavailable = client.get("/ready")
        assert unavailable.status_code == 503
        assert unavailable.json()["status"] == "storage_unavailable"
        remote_host = client.get("/ready", headers={"Host": "example.invalid"})
        assert remote_host.status_code == 404
