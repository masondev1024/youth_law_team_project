"""Minimal Lambda gateway for conditional DynamoDB deletion-intent writes.

This file is packaged alone. It intentionally has no application imports and
supports only `append`; control initialization, takeover, and deletion are
operator-only operations outside this function.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INSTANCE_ID = re.compile(r"^i-[0-9a-f]{8,17}$")
_MAX_INTENTS = 1000
_TABLE_ENV = "AUTHORITY_TABLE"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _AuthorityFailure(Exception):
    def __init__(self, code: str):
        self.code = code


def _canonical_expiry(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)
    except (TypeError, ValueError):
        return False
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z") == value


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(value)}


def _string(item: dict[str, Any], field: str) -> str:
    value = item.get(field)
    if type(value) is not dict or set(value) != {"S"} or not isinstance(value["S"], str):
        raise _AuthorityFailure("invalid_authority")
    return value["S"]


def _integer(item: dict[str, Any], field: str) -> int:
    value = item.get(field)
    if type(value) is not dict or set(value) != {"N"} or not isinstance(value["N"], str):
        raise _AuthorityFailure("invalid_authority")
    raw = value["N"]
    if not re.fullmatch(r"0|[1-9][0-9]{0,11}", raw):
        raise _AuthorityFailure("invalid_authority")
    return int(raw)


def _dynamodb_error(exc: Exception) -> _AuthorityFailure:
    response = getattr(exc, "response", None)
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    if code in {"AccessDeniedException", "AccessDenied", "UnauthorizedOperation"}:
        return _AuthorityFailure("authority_denied")
    if code in {"TransactionCanceledException", "ConditionalCheckFailedException"}:
        return _AuthorityFailure("transaction_conflict")
    return _AuthorityFailure("authority_unavailable")


def _read_item(client: Any, table: str, pk: str, sk: str) -> dict[str, Any] | None:
    try:
        response = client.get_item(
            TableName=table,
            Key={"pk": _s(pk), "sk": _s(sk)},
            ConsistentRead=True,
        )
    except Exception as exc:
        raise _dynamodb_error(exc) from exc
    if type(response) is not dict:
        raise _AuthorityFailure("invalid_authority")
    item = response.get("Item")
    if item is None:
        return None
    if type(item) is not dict:
        raise _AuthorityFailure("invalid_authority")
    return item


def _control(
    client: Any,
    table: str,
    pk: str,
    fingerprint: str,
    owner: str,
    epoch: int,
) -> dict[str, Any]:
    item = _read_item(client, table, pk, "CONTROL")
    if item is None:
        raise _AuthorityFailure("authority_missing")
    values = {
        "pk": _string(item, "pk"),
        "sk": _string(item, "sk"),
        "kind": _string(item, "kind"),
        "dataset_fingerprint": _string(item, "dataset_fingerprint"),
        "owner_instance_id": _string(item, "owner_instance_id"),
        "epoch": _integer(item, "epoch"),
        "highwater": _integer(item, "highwater"),
    }
    if (
        values["pk"] != pk
        or values["sk"] != "CONTROL"
        or values["kind"] != "control"
        or values["dataset_fingerprint"] != fingerprint
        or not _INSTANCE_ID.fullmatch(values["owner_instance_id"])
        or values["epoch"] < 1
        or values["highwater"] > _MAX_INTENTS
    ):
        raise _AuthorityFailure("invalid_authority")
    if values["owner_instance_id"] != owner:
        raise _AuthorityFailure("owner_mismatch")
    if values["epoch"] != epoch:
        raise _AuthorityFailure("stale_epoch")
    return values


def _existing_intent(
    client: Any,
    table: str,
    pk: str,
    fingerprint: str,
    request_hash: str,
) -> tuple[str, int] | None:
    item = _read_item(client, table, pk, f"INTENT#{request_hash}")
    if item is None:
        return None
    stored = {
        "pk": _string(item, "pk"),
        "sk": _string(item, "sk"),
        "kind": _string(item, "kind"),
        "dataset_fingerprint": _string(item, "dataset_fingerprint"),
        "request_key_hash": _string(item, "request_key_hash"),
        "expires_at": _string(item, "expires_at"),
        "sequence": _integer(item, "sequence"),
    }
    if (
        stored["pk"] != pk
        or stored["sk"] != f"INTENT#{request_hash}"
        or stored["kind"] != "intent"
        or stored["dataset_fingerprint"] != fingerprint
        or stored["request_key_hash"] != request_hash
        or not _SHA256.fullmatch(stored["request_key_hash"])
        or not _canonical_expiry(stored["expires_at"])
        or not 1 <= stored["sequence"] <= _MAX_INTENTS
    ):
        raise _AuthorityFailure("invalid_authority")
    return stored["expires_at"], stored["sequence"]


def _append(event: object, client: Any, table: str) -> dict[str, Any]:
    fields = {
        "operation",
        "dataset_fingerprint",
        "owner_instance_id",
        "epoch",
        "request_key_hash",
        "expires_at",
    }
    if type(event) is not dict or set(event) != fields:
        raise _AuthorityFailure("invalid_record")
    fingerprint = event["dataset_fingerprint"]
    owner = event["owner_instance_id"]
    epoch = event["epoch"]
    request_hash = event["request_key_hash"]
    expires_at = event["expires_at"]
    if (
        event["operation"] != "append"
        or not isinstance(fingerprint, str)
        or not _SHA256.fullmatch(fingerprint)
        or not isinstance(owner, str)
        or not _INSTANCE_ID.fullmatch(owner)
        or type(epoch) is not int
        or epoch < 1
        or not isinstance(request_hash, str)
        or not _SHA256.fullmatch(request_hash)
        or not _canonical_expiry(expires_at)
    ):
        raise _AuthorityFailure("invalid_record")

    pk = f"DATASET#{fingerprint}"
    control = _control(client, table, pk, fingerprint, owner, epoch)
    prior = _existing_intent(client, table, pk, fingerprint, request_hash)
    if prior is not None:
        prior_expiry, prior_sequence = prior
        if prior_expiry != expires_at:
            raise _AuthorityFailure("intent_conflict")
        if prior_sequence > control["highwater"]:
            raise _AuthorityFailure("invalid_authority")
        return {"ok": True, "sequence": prior_sequence}
    expires = datetime.fromisoformat(expires_at[:-1] + "+00:00").astimezone(timezone.utc)
    now = _utc_now()
    if expires <= now or expires > now + timedelta(days=30, seconds=5):
        raise _AuthorityFailure("invalid_record")
    if control["highwater"] >= _MAX_INTENTS:
        raise _AuthorityFailure("authority_capacity")

    sequence = control["highwater"] + 1
    item = {
        "pk": _s(pk),
        "sk": _s(f"INTENT#{request_hash}"),
        "kind": _s("intent"),
        "dataset_fingerprint": _s(fingerprint),
        "request_key_hash": _s(request_hash),
        "expires_at": _s(expires_at),
        "sequence": _n(sequence),
    }
    transact_items = [
        {
            "Put": {
                "TableName": table,
                "Item": item,
                "ConditionExpression": "attribute_not_exists(#pk)",
                "ExpressionAttributeNames": {"#pk": "pk"},
            }
        },
        {
            "Update": {
                "TableName": table,
                "Key": {"pk": _s(pk), "sk": _s("CONTROL")},
                "UpdateExpression": "SET #highwater = :next",
                "ConditionExpression": (
                    "#owner = :owner AND #epoch = :epoch AND #highwater = :current "
                    "AND #fingerprint = :fingerprint AND #kind = :control"
                ),
                "ExpressionAttributeNames": {
                    "#owner": "owner_instance_id",
                    "#epoch": "epoch",
                    "#highwater": "highwater",
                    "#fingerprint": "dataset_fingerprint",
                    "#kind": "kind",
                },
                "ExpressionAttributeValues": {
                    ":owner": _s(owner),
                    ":epoch": _n(epoch),
                    ":current": _n(control["highwater"]),
                    ":next": _n(sequence),
                    ":fingerprint": _s(fingerprint),
                    ":control": _s("control"),
                },
            }
        },
    ]
    request_token = hashlib.sha256(
        f"{fingerprint}:{owner}:{epoch}:{control['highwater']}:{request_hash}:{expires_at}".encode(
            "ascii"
        )
    ).hexdigest()[:36]
    try:
        client.transact_write_items(
            TransactItems=transact_items,
            ClientRequestToken=request_token,
        )
        return {"ok": True, "sequence": sequence}
    except Exception as exc:
        # DynamoDB may commit while the response is lost. Resolve with a
        # strongly consistent control and intent read; never repeat the write.
        try:
            current = _control(client, table, pk, fingerprint, owner, epoch)
            committed = _existing_intent(client, table, pk, fingerprint, request_hash)
        except _AuthorityFailure as read_error:
            raise read_error from exc
        if committed is not None:
            committed_expiry, committed_sequence = committed
            if (
                committed_expiry == expires_at
                and committed_sequence <= current["highwater"]
                and committed_sequence >= sequence
            ):
                return {"ok": True, "sequence": committed_sequence}
            raise _AuthorityFailure("invalid_authority") from exc
        raise _dynamodb_error(exc) from exc


def handle_event(event: object, client: Any, table_name: str) -> dict[str, Any]:
    """Testable handler body; table selection remains outside event input."""
    if not isinstance(table_name, str) or not table_name:
        return {"ok": False, "error": "invalid_configuration"}
    try:
        return _append(event, client, table_name)
    except _AuthorityFailure as exc:
        return {"ok": False, "error": exc.code}
    except Exception:
        return {"ok": False, "error": "authority_unavailable"}


def lambda_handler(event: object, context: Any) -> dict[str, Any]:
    table_name = os.environ.get(_TABLE_ENV)
    if not table_name:
        return {"ok": False, "error": "invalid_configuration"}
    try:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "dynamodb",
            config=Config(retries={"total_max_attempts": 1, "mode": "standard"}),
        )
    except Exception:
        return {"ok": False, "error": "authority_unavailable"}
    return handle_event(event, client, table_name)
