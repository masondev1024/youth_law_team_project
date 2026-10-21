from __future__ import annotations

import hashlib
import io
import json

import pytest

from app.cloud_authority_handler import handle_event
from app.cloud_deletion_authority import (
    CloudAuthorityError,
    CloudDeletionAuthority,
    LambdaDeletionWriter,
)

_FINGERPRINT = hashlib.sha256(b"dataset marker").hexdigest()
_OWNER = "i-0123456789abcdef0"
_OTHER_OWNER = "i-abcdef01234567890"
_EXPIRY = "2026-11-19T12:00:00.000000Z"


def s(value):
    return {"S": value}


def n(value):
    return {"N": str(value)}


def control(owner=_OWNER, epoch=1, highwater=0, fingerprint=_FINGERPRINT):
    return {
        "pk": s(f"DATASET#{fingerprint}"),
        "sk": s("CONTROL"),
        "kind": s("control"),
        "dataset_fingerprint": s(fingerprint),
        "owner_instance_id": s(owner),
        "epoch": n(epoch),
        "highwater": n(highwater),
    }


class DynamoError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class FakeDynamo:
    def __init__(self, control_item=None, *, page_size=1000):
        self.items = {}
        self.page_size = page_size
        self.commit_then_timeout = False
        self.transaction_error = None
        self.read_error = None
        if control_item is not None:
            self.items[(control_item["pk"]["S"], control_item["sk"]["S"])] = control_item

    def get_item(self, *, TableName, Key, ConsistentRead):
        assert TableName == "authority"
        assert ConsistentRead is True
        if self.read_error:
            raise self.read_error
        return {"Item": self.items.get((Key["pk"]["S"], Key["sk"]["S"]))}

    def transact_write_items(self, *, TransactItems, ClientRequestToken=None):
        put, update = TransactItems
        put_op = put["Put"]
        update_op = update["Update"]
        item = put_op["Item"]
        key = (item["pk"]["S"], item["sk"]["S"])
        control_key = (update_op["Key"]["pk"]["S"], update_op["Key"]["sk"]["S"])
        current_control = self.items.get(control_key)
        values = update_op["ExpressionAttributeValues"]
        expected = (
            values[":owner"]["S"],
            int(values[":epoch"]["N"]),
            int(values[":current"]["N"]),
        )
        actual = (
            current_control["owner_instance_id"]["S"],
            int(current_control["epoch"]["N"]),
            int(current_control["highwater"]["N"]),
        )
        if key in self.items or actual != expected or self.transaction_error:
            raise self.transaction_error or DynamoError("TransactionCanceledException")
        self.items[key] = item
        current_control["highwater"] = values[":next"]
        if self.commit_then_timeout:
            self.commit_then_timeout = False
            raise DynamoError("Timeout")
        return {}

    def query(self, *, TableName, KeyConditionExpression, ExpressionAttributeNames,
              ExpressionAttributeValues, ConsistentRead, Limit, ExclusiveStartKey=None):
        assert TableName == "authority"
        assert ConsistentRead is True
        pk = ExpressionAttributeValues[":pk"]["S"]
        prefix = ExpressionAttributeValues[":prefix"]["S"]
        rows = [
            item for (row_pk, row_sk), item in self.items.items()
            if row_pk == pk and row_sk.startswith(prefix)
        ]
        rows.sort(key=lambda item: item["sk"]["S"])
        if ExclusiveStartKey:
            after = ExclusiveStartKey["sk"]["S"]
            rows = [item for item in rows if item["sk"]["S"] > after]
        page = rows[: min(self.page_size, Limit)]
        response = {"Items": page}
        if len(rows) > len(page):
            last = page[-1]
            response["LastEvaluatedKey"] = {"pk": last["pk"], "sk": last["sk"]}
        return response


class FakeLambda:
    def __init__(self, dynamodb):
        self.dynamodb = dynamodb
        self.calls = []

    def invoke(self, *, FunctionName, InvocationType, Payload):
        assert InvocationType == "RequestResponse"
        self.calls.append((FunctionName, json.loads(Payload)))
        result = handle_event(json.loads(Payload), self.dynamodb, "authority")
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(result).encode())}


def make_authority(fake):
    return CloudDeletionAuthority(fake, "authority", _FINGERPRINT, _OWNER)


def request_hash(value="request-1"):
    return hashlib.sha256(value.encode()).hexdigest()


def test_transaction_timeout_after_commit_is_resolved_by_strong_snapshot():
    fake = FakeDynamo(control(), page_size=1)
    fake.commit_then_timeout = True
    authority = make_authority(fake)

    assert authority.append(request_hash(), _EXPIRY) == 1
    intents, highwater = authority.snapshot()
    assert highwater == 1
    assert intents[0].request_key_hash == request_hash()
    assert intents[0].expires_at == _EXPIRY
    assert set(fake.items[(f"DATASET#{_FINGERPRINT}", f"INTENT#{request_hash()}")]) == {
        "pk", "sk", "kind", "dataset_fingerprint", "request_key_hash", "expires_at", "sequence"
    }


def test_idempotent_replay_preserves_original_sequence_and_expiry():
    authority = make_authority(FakeDynamo(control()))
    digest = request_hash()
    assert authority.append(digest, _EXPIRY) == 1
    assert authority.append(digest, _EXPIRY) == 1
    with pytest.raises(CloudAuthorityError) as error:
        authority.append(digest, "2026-11-21T12:00:00.000000Z")
    assert error.value.code == "intent_conflict"


@pytest.mark.parametrize(
    "item,owner,code",
    [
        (None, _OWNER, "authority_missing"),
        (control(owner=_OTHER_OWNER), _OWNER, "owner_mismatch"),
    ],
)
def test_missing_or_other_owner_fails_closed(item, owner, code):
    fake = FakeDynamo(item)
    with pytest.raises(CloudAuthorityError) as error:
        CloudDeletionAuthority(fake, "authority", _FINGERPRINT, owner)
    assert error.value.code == code


def test_pinned_epoch_rejects_old_process_after_operator_takeover():
    fake = FakeDynamo(control())
    authority = make_authority(fake)
    fake.items[(f"DATASET#{_FINGERPRINT}", "CONTROL")]["owner_instance_id"] = s(_OTHER_OWNER)
    fake.items[(f"DATASET#{_FINGERPRINT}", "CONTROL")]["epoch"] = n(2)
    with pytest.raises(CloudAuthorityError) as error:
        authority.assert_current()
    assert error.value.code == "owner_mismatch"


def test_pinned_epoch_rejects_old_epoch_for_same_instance():
    fake = FakeDynamo(control())
    authority = make_authority(fake)
    fake.items[(f"DATASET#{_FINGERPRINT}", "CONTROL")]["epoch"] = n(2)
    with pytest.raises(CloudAuthorityError) as error:
        authority.assert_current()
    assert error.value.code == "stale_epoch"


def test_transaction_cas_conflict_returns_safe_conflict():
    fake = FakeDynamo(control())
    authority = make_authority(fake)
    fake.transaction_error = DynamoError("TransactionCanceledException")
    with pytest.raises(CloudAuthorityError) as error:
        authority.append(request_hash(), _EXPIRY)
    assert error.value.code == "transaction_conflict"
    assert fake.items[(f"DATASET#{_FINGERPRINT}", "CONTROL")]["highwater"] == n(0)


def test_paginated_snapshot_detects_missing_sequence():
    fake = FakeDynamo(control(highwater=2), page_size=1)
    first = request_hash("first")
    third = request_hash("third")
    fake.items[(f"DATASET#{_FINGERPRINT}", f"INTENT#{first}")] = {
        "pk": s(f"DATASET#{_FINGERPRINT}"), "sk": s(f"INTENT#{first}"),
        "kind": s("intent"), "dataset_fingerprint": s(_FINGERPRINT),
        "request_key_hash": s(first), "expires_at": s(_EXPIRY), "sequence": n(1),
    }
    fake.items[(f"DATASET#{_FINGERPRINT}", f"INTENT#{third}")] = {
        "pk": s(f"DATASET#{_FINGERPRINT}"), "sk": s(f"INTENT#{third}"),
        "kind": s("intent"), "dataset_fingerprint": s(_FINGERPRINT),
        "request_key_hash": s(third), "expires_at": s(_EXPIRY), "sequence": n(3),
    }
    with pytest.raises(CloudAuthorityError) as error:
        make_authority(fake).snapshot()
    assert error.value.code == "invalid_authority"


def test_append_refuses_to_extend_inconsistent_highwater():
    fake = FakeDynamo(control(highwater=1))
    with pytest.raises(CloudAuthorityError) as error:
        make_authority(fake).append(request_hash(), _EXPIRY)
    assert error.value.code == "invalid_authority"
    assert fake.items[(f"DATASET#{_FINGERPRINT}", "CONTROL")]["highwater"] == n(1)


def test_malformed_values_and_dynamodb_failures_are_sanitized():
    malformed = control()
    malformed["highwater"] = {"N": "1.5"}
    with pytest.raises(CloudAuthorityError) as error:
        make_authority(FakeDynamo(malformed))
    assert error.value.code == "invalid_authority"

    fake = FakeDynamo(control())
    authority = make_authority(fake)
    fake.read_error = RuntimeError("sensitive implementation detail")
    with pytest.raises(CloudAuthorityError) as error:
        authority.assert_current()
    assert str(error.value) == "authority_unavailable"
    assert "sensitive" not in str(error.value)


def test_lambda_writer_is_used_then_strong_read_verifies_append():
    fake = FakeDynamo(control())
    lambda_client = FakeLambda(fake)
    writer = LambdaDeletionWriter(lambda_client, "authority-writer")
    authority = CloudDeletionAuthority(
        fake,
        "authority",
        _FINGERPRINT,
        _OWNER,
        writer=writer,
    )

    assert authority.append(request_hash(), _EXPIRY) == 1
    assert lambda_client.calls == [
        (
            "authority-writer",
            {
                "operation": "append",
                "dataset_fingerprint": _FINGERPRINT,
                "owner_instance_id": _OWNER,
                "epoch": 1,
                "request_key_hash": request_hash(),
                "expires_at": _EXPIRY,
            },
        )
    ]


def test_lambda_writer_timeout_after_commit_is_verified_without_retry():
    fake = FakeDynamo(control())
    fake.commit_then_timeout = True
    lambda_client = FakeLambda(fake)
    authority = CloudDeletionAuthority(
        fake,
        "authority",
        _FINGERPRINT,
        _OWNER,
        writer=LambdaDeletionWriter(lambda_client, "authority-writer"),
    )

    assert authority.append(request_hash(), _EXPIRY) == 1
    assert len(lambda_client.calls) == 1
    assert fake.items[(f"DATASET#{_FINGERPRINT}", "CONTROL")]["highwater"] == n(1)


def test_lambda_writer_result_without_committed_intent_fails_closed():
    class LyingWriter:
        def append(self, *args):
            return 1

    authority = CloudDeletionAuthority(
        FakeDynamo(control()),
        "authority",
        _FINGERPRINT,
        _OWNER,
        writer=LyingWriter(),
    )
    with pytest.raises(CloudAuthorityError) as error:
        authority.append(request_hash(), _EXPIRY)
    assert error.value.code == "invalid_authority"


def test_lambda_writer_accepts_sequence_shift_from_concurrent_append():
    fake = FakeDynamo(control())

    class ConcurrentWriter:
        def append(self, fingerprint, owner, epoch, digest, expires_at):
            other = {
                "operation": "append",
                "dataset_fingerprint": fingerprint,
                "owner_instance_id": owner,
                "epoch": epoch,
                "request_key_hash": request_hash("concurrent"),
                "expires_at": expires_at,
            }
            assert handle_event(other, fake, "authority") == {"ok": True, "sequence": 1}
            own = {
                **other,
                "request_key_hash": digest,
                "expires_at": expires_at,
            }
            result = handle_event(own, fake, "authority")
            return result["sequence"]

    authority = CloudDeletionAuthority(
        fake,
        "authority",
        _FINGERPRINT,
        _OWNER,
        writer=ConcurrentWriter(),
    )
    assert authority.append(request_hash(), _EXPIRY) == 2
