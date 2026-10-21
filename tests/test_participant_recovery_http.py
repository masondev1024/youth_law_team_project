"""Restoring a pre-withdrawal snapshot cannot expose deleted API records."""

import hashlib
import os
import sqlite3

import pytest
from test_rehearsal_app import (
    action,
    grant_record,
    local_client,
    login,
    make_app,
    make_runtime,
    observation,
    state,
)

from app.participant_recovery import restore_database
from app.study_storage import StudyStorageError

pytest_plugins = ("test_rehearsal_app",)


def test_old_backup_restores_only_retained_observation_through_api(private_root, tmp_path):
    runtime = make_runtime(private_root)
    store = runtime.participant_store
    with local_client(make_app(private_root, runtime)) as client:
        first, _ = login(client)
        first_record = grant_record(client, first)["record"]
        deleted_id = first_record["record_id"]
        saved = action(
            client,
            "/rehearsal/api/observations",
            {"expected_version": 1, "observation": observation()},
            first["csrf_token"],
        )
        assert saved.status_code == 200
        first_cookies = dict(client.cookies)

        second, _ = login(client)
        retained_id = grant_record(client, second)["record"]["record_id"]
        retained_observation = observation(stuck_event_count_during_task=1)
        saved = action(
            client,
            "/rehearsal/api/observations",
            {"expected_version": 1, "observation": retained_observation},
            second["csrf_token"],
        )
        assert saved.status_code == 200

        backup = tmp_path / "before-withdrawal.sqlite3"
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        with sqlite3.connect(store.database.as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(backup) as target:
                source.backup(target)
        backup_hash = hashlib.sha256(backup.read_bytes()).hexdigest()

        client.cookies.clear()
        client.cookies.update(first_cookies)
        withdrawn = action(client, "/rehearsal/api/withdraw", {}, first["csrf_token"])
        assert withdrawn.status_code == 200
        checkpoint = store.current_ledger_checkpoint()
        before_attempt = store.database.read_bytes()
        with pytest.raises(StudyStorageError):
            restore_database(store, backup, expected_ledger_checkpoint=checkpoint)
        assert store.database.read_bytes() == before_attempt

    result = restore_database(store, backup, expected_ledger_checkpoint=checkpoint)
    assert result["records_withdrawn"] == 1
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == backup_hash

    restarted_runtime = make_runtime(private_root)
    with local_client(make_app(private_root, restarted_runtime)) as client:
        current, _ = login(client)
        assert current["session_admission"] is False
        assert [row["record_id"] for row in current["available_records"]] == [retained_id]
        denied = action(
            client, "/rehearsal/api/restore", {"record_id": deleted_id}, current["csrf_token"]
        )
        assert denied.status_code == 410
        restored = action(
            client, "/rehearsal/api/restore", {"record_id": retained_id}, current["csrf_token"]
        )
        assert restored.status_code == 200
        assert restored.json()["record"]["version"] == 2
        assert restored.json()["record"]["observation"] == retained_observation
        assert state(client)["record"]["record_id"] == retained_id
