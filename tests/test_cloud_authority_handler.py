from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from app import cloud_authority_handler
from app.cloud_authority_handler import handle_event

_FINGERPRINT = hashlib.sha256(b"dataset marker").hexdigest()
_OWNER = "i-0123456789abcdef0"
_OTHER_OWNER = "i-abcdef01234567890"
_HASH = hashlib.sha256(b"opaque request key").hexdigest()
_EXPIRY = "2026-11-20T12:00:00.000000Z"
_TABLE = "authority"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(
        cloud_authority_handler,
        "_utc_now",
        lambda: datetime(2026, 10, 21, 12, tzinfo=timezone.utc),
    )


def s(value):
    return {"S": value}


def n(value):
    return {"N": str(value)}


def control(owner=_OWNER, epoch=1, highwater=0):
    return {
        "pk": s(f"DATASET#{_FINGERPRINT}"),
        "sk": s("CONTROL"),
        "kind": s("control"),
        "dataset_fingerprint": s(_FINGERPRINT),
        "owner_instance_id": s(owner),
        "epoch": n(epoch),
        "highwater": n(highwater),
    }


class DynamoError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class FakeDynamo:
    def __init__(self, control_item=None):
        self.items = {}
        self.write_count = 0
        self.commit_then_timeout = False
        self.transaction_error = None
        self.read_error = None
        if control_item is not None:
            self.items[(control_item["pk"]["S"], control_item["sk"]["S"])] = control_item

    def get_item(self, *, TableName, Key, ConsistentRead):
        assert TableName == _TABLE
        assert ConsistentRead is True
        if self.read_error:
            raise self.read_error
        return {"Item": self.items.get((Key["pk"]["S"], Key["sk"]["S"]))}

    def transact_write_items(self, *, TransactItems, ClientRequestToken):
        self.write_count += 1
        assert len(ClientRequestToken) == 36
        put_op = TransactItems[0]["Put"]
        update_op = TransactItems[1]["Update"]
        put_item = put_op["Item"]
        put_key = (put_item["pk"]["S"], put_item["sk"]["S"])
        update_key = (update_op["Key"]["pk"]["S"], update_op["Key"]["sk"]["S"])
        control_item = self.items.get(update_key)
        values = update_op["ExpressionAttributeValues"]
        expected = (
            values[":owner"]["S"],
            int(values[":epoch"]["N"]),
            int(values[":current"]["N"]),
        )
        actual = (
            control_item["owner_instance_id"]["S"],
            int(control_item["epoch"]["N"]),
            int(control_item["highwater"]["N"]),
        )
        assert update_op["UpdateExpression"] == "SET #highwater = :next"
        assert "owner_instance_id" in update_op["ExpressionAttributeNames"].values()
        assert "epoch" in update_op["ExpressionAttributeNames"].values()
        assert put_op["ConditionExpression"] == "attribute_not_exists(#pk)"
        if put_key in self.items or actual != expected or self.transaction_error:
            raise self.transaction_error or DynamoError("TransactionCanceledException")
        self.items[put_key] = put_item
        control_item["highwater"] = values[":next"]
        if self.commit_then_timeout:
            self.commit_then_timeout = False
            raise DynamoError("Timeout")
        return {}


def event(**updates):
    value = {
        "operation": "append",
        "dataset_fingerprint": _FINGERPRINT,
        "owner_instance_id": _OWNER,
        "epoch": 1,
        "request_key_hash": _HASH,
        "expires_at": _EXPIRY,
    }
    value.update(updates)
    return value


def invoke(fake, value=None):
    return handle_event(event() if value is None else value, fake, _TABLE)


def test_append_writes_only_hash_expiry_and_sequence_in_one_transaction():
    fake = FakeDynamo(control())

    result = invoke(fake)

    assert result == {"ok": True, "sequence": 1}
    assert fake.write_count == 1
    stored = fake.items[(f"DATASET#{_FINGERPRINT}", f"INTENT#{_HASH}")]
    assert stored == {
        "pk": s(f"DATASET#{_FINGERPRINT}"),
        "sk": s(f"INTENT#{_HASH}"),
        "kind": s("intent"),
        "dataset_fingerprint": s(_FINGERPRINT),
        "request_key_hash": s(_HASH),
        "expires_at": s(_EXPIRY),
        "sequence": n(1),
    }


def test_append_retry_is_idempotent_and_expiry_cannot_change():
    fake = FakeDynamo(control())
    assert invoke(fake) == {"ok": True, "sequence": 1}
    assert invoke(fake) == {"ok": True, "sequence": 1}
    assert fake.write_count == 1
    result = invoke(fake, event(expires_at="2026-11-21T12:00:00.000000Z"))
    assert result == {"ok": False, "error": "intent_conflict"}
    assert fake.write_count == 1


def test_existing_intent_retry_keeps_original_expiry_after_expiration(monkeypatch):
    fake = FakeDynamo(control())
    assert invoke(fake) == {"ok": True, "sequence": 1}
    monkeypatch.setattr(
        cloud_authority_handler,
        "_utc_now",
        lambda: datetime(2026, 12, 1, 12, tzinfo=timezone.utc),
    )

    assert invoke(fake) == {"ok": True, "sequence": 1}
    assert fake.write_count == 1


@pytest.mark.parametrize(
    "item,request_event,expected",
    [
        (None, event(), "authority_missing"),
        (control(owner=_OTHER_OWNER), event(), "owner_mismatch"),
        (control(epoch=2), event(), "stale_epoch"),
        (control(highwater=1000), event(), "authority_capacity"),
        (control(), event(epoch=True), "invalid_record"),
        (control(), event(expires_at="2026-11-20T12:00:00Z"), "invalid_record"),
        (control(), event(expires_at="2026-10-21T11:59:59.000000Z"), "invalid_record"),
        (control(), event(expires_at="2026-11-21T12:00:00.000000Z"), "invalid_record"),
    ],
)
def test_append_validates_authority_and_event(item, request_event, expected):
    result = invoke(FakeDynamo(item), request_event)
    assert result == {"ok": False, "error": expected}


def test_only_append_operation_and_exact_event_schema_are_supported():
    fake = FakeDynamo(control())
    assert invoke(fake, event(operation="initialize")) == {
        "ok": False,
        "error": "invalid_record",
    }
    assert invoke(fake, event(table_name="attacker-table")) == {
        "ok": False,
        "error": "invalid_record",
    }
    assert fake.write_count == 0


def test_transaction_cas_conflict_and_timeout_resolution_are_safe():
    conflict = FakeDynamo(control())
    conflict.transaction_error = DynamoError("TransactionCanceledException")
    assert invoke(conflict) == {"ok": False, "error": "transaction_conflict"}
    assert conflict.items[(f"DATASET#{_FINGERPRINT}", "CONTROL")]["highwater"] == n(0)

    committed = FakeDynamo(control())
    committed.commit_then_timeout = True
    assert invoke(committed) == {"ok": True, "sequence": 1}
    assert committed.write_count == 1
    assert committed.items[(f"DATASET#{_FINGERPRINT}", "CONTROL")]["highwater"] == n(1)


def test_permission_failure_is_sanitized_without_echoing_request():
    fake = FakeDynamo(control())
    fake.read_error = DynamoError("AccessDeniedException")

    result = invoke(fake)

    assert result == {"ok": False, "error": "authority_denied"}
    assert "opaque" not in repr(result)
    assert fake.write_count == 0
