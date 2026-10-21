"""DynamoDB authority for deletion intents outside restorable local storage.

This adapter never creates or takes over the control record. An operator must
initialize it and, for a new instance, conditionally advance its epoch after
verifying that the old instance is terminated.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.study_storage import StudyStorageError

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_INSTANCE_ID = re.compile(r"^i-[0-9a-f]{8,17}$")
_MAX_INTENTS = 1000
_CONTROL_SK = "CONTROL"


class CloudAuthorityError(StudyStorageError):
    """Safe, content-free failure from the remote deletion authority."""

    def __init__(self, code: str):
        super().__init__(code)


@dataclass(frozen=True)
class DeletionIntent:
    request_key_hash: str
    expires_at: str
    sequence: int


class LambdaDeletionWriter:
    """Invoke the narrowly scoped write gateway with one bounded append."""

    def __init__(self, lambda_client: Any, function_name: str) -> None:
        if not isinstance(function_name, str) or not function_name:
            raise CloudAuthorityError("invalid_configuration")
        self.lambda_client = lambda_client
        self.function_name = function_name

    def append(
        self,
        dataset_fingerprint: str,
        owner_instance_id: str,
        epoch: int,
        request_key_hash: str,
        expires_at: str,
    ) -> int:
        event = {
            "operation": "append",
            "dataset_fingerprint": dataset_fingerprint,
            "owner_instance_id": owner_instance_id,
            "epoch": epoch,
            "request_key_hash": request_key_hash,
            "expires_at": expires_at,
        }
        try:
            response = self.lambda_client.invoke(
                FunctionName=self.function_name,
                InvocationType="RequestResponse",
                Payload=json.dumps(event, sort_keys=True, separators=(",", ":")).encode(
                    "ascii"
                ),
            )
            payload = response.get("Payload") if type(response) is dict else None
            if (
                payload is None
                or response.get("StatusCode") != 200
                or response.get("FunctionError")
            ):
                raise CloudAuthorityError("authority_write_failed")
            raw = payload.read(65537)
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            if not isinstance(raw, bytes) or len(raw) > 65536:
                raise CloudAuthorityError("authority_write_failed")
            result = json.loads(raw.decode("utf-8"))
        except CloudAuthorityError:
            raise
        except Exception as exc:
            raise CloudAuthorityError("authority_write_failed") from exc
        if type(result) is not dict:
            raise CloudAuthorityError("authority_write_failed")
        if result.get("ok") is False:
            code = result.get("error")
            if isinstance(code, str) and code in {
                "authority_missing",
                "authority_unavailable",
                "authority_denied",
                "owner_mismatch",
                "stale_epoch",
                "invalid_authority",
                "invalid_record",
                "intent_conflict",
                "transaction_conflict",
                "authority_capacity",
            }:
                raise CloudAuthorityError(code)
            raise CloudAuthorityError("authority_write_failed")
        if set(result) != {"ok", "sequence"} or result.get("ok") is not True:
            raise CloudAuthorityError("authority_write_failed")
        sequence = result.get("sequence")
        if type(sequence) is not int or not 1 <= sequence <= _MAX_INTENTS:
            raise CloudAuthorityError("invalid_authority")
        return sequence


def _canonical_expiry(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)
    except (TypeError, ValueError):
        return False
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z") == value


def _av_string(item: dict[str, Any], name: str) -> str:
    value = item.get(name)
    if type(value) is not dict or set(value) != {"S"} or not isinstance(value["S"], str):
        raise CloudAuthorityError("invalid_authority")
    return value["S"]


def _av_int(item: dict[str, Any], name: str) -> int:
    value = item.get(name)
    if type(value) is not dict or set(value) != {"N"} or not isinstance(value["N"], str):
        raise CloudAuthorityError("invalid_authority")
    raw = value["N"]
    if not re.fullmatch(r"0|[1-9][0-9]{0,11}", raw):
        raise CloudAuthorityError("invalid_authority")
    return int(raw)


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(value)}


class CloudDeletionAuthority:
    """Strongly consistent, bounded deletion authority backed by a base table.

    Table keys are `pk` and `sk`. Each dataset partition has one operator-owned
    `CONTROL` row and at most 1,000 `INTENT#{sha256}` rows. The first control
    read pins this process to the observed owner and epoch for its lifetime.
    Supplying a writer routes appends through the narrow Lambda gateway; the
    direct transaction path exists for local tests and controlled tooling.
    """

    def __init__(
        self,
        client: Any,
        table_name: str,
        dataset_fingerprint: str,
        owner_instance_id: str,
        *,
        writer: Any | None = None,
    ) -> None:
        if (
            not isinstance(table_name, str)
            or not table_name
            or not isinstance(dataset_fingerprint, str)
            or not _SHA256.fullmatch(dataset_fingerprint)
            or not isinstance(owner_instance_id, str)
            or not _INSTANCE_ID.fullmatch(owner_instance_id)
        ):
            raise CloudAuthorityError("invalid_configuration")
        self.client = client
        self.table_name = table_name
        self.dataset_fingerprint = dataset_fingerprint
        self.owner_instance_id = owner_instance_id
        self.writer = writer
        if writer is not None and not callable(getattr(writer, "append", None)):
            raise CloudAuthorityError("invalid_configuration")
        self.pk = f"DATASET#{dataset_fingerprint}"
        control = self._read_control()
        self.epoch = control["epoch"]

    def _key(self, sk: str) -> dict[str, dict[str, str]]:
        return {"pk": _s(self.pk), "sk": _s(sk)}

    def _read_item(self, key: dict[str, dict[str, str]]) -> dict[str, Any] | None:
        try:
            response = self.client.get_item(
                TableName=self.table_name, Key=key, ConsistentRead=True
            )
        except Exception as exc:
            raise CloudAuthorityError("authority_unavailable") from exc
        if type(response) is not dict:
            raise CloudAuthorityError("invalid_authority")
        item = response.get("Item")
        if item is None:
            return None
        if type(item) is not dict:
            raise CloudAuthorityError("invalid_authority")
        return item

    def _read_control(self) -> dict[str, Any]:
        item = self._read_item(self._key(_CONTROL_SK))
        if item is None:
            raise CloudAuthorityError("authority_missing")
        try:
            values = {
                "pk": _av_string(item, "pk"),
                "sk": _av_string(item, "sk"),
                "kind": _av_string(item, "kind"),
                "dataset_fingerprint": _av_string(item, "dataset_fingerprint"),
                "owner_instance_id": _av_string(item, "owner_instance_id"),
                "epoch": _av_int(item, "epoch"),
                "highwater": _av_int(item, "highwater"),
            }
        except (KeyError, TypeError) as exc:
            raise CloudAuthorityError("invalid_authority") from exc
        if (
            values["pk"] != self.pk
            or values["sk"] != _CONTROL_SK
            or values["kind"] != "control"
            or values["dataset_fingerprint"] != self.dataset_fingerprint
            or not _INSTANCE_ID.fullmatch(values["owner_instance_id"])
            or values["epoch"] < 1
            or values["highwater"] > _MAX_INTENTS
        ):
            raise CloudAuthorityError("invalid_authority")
        if values["owner_instance_id"] != self.owner_instance_id:
            raise CloudAuthorityError("owner_mismatch")
        if hasattr(self, "epoch") and values["epoch"] != self.epoch:
            raise CloudAuthorityError("stale_epoch")
        return values

    def assert_current(self) -> int:
        """Confirm that this process still owns the pinned dataset epoch."""
        return self._read_control()["epoch"]

    def _read_intent(self, request_key_hash: str) -> DeletionIntent | None:
        item = self._read_item(self._key(f"INTENT#{request_key_hash}"))
        if item is None:
            return None
        try:
            pk = _av_string(item, "pk")
            sk = _av_string(item, "sk")
            kind = _av_string(item, "kind")
            fingerprint = _av_string(item, "dataset_fingerprint")
            stored_hash = _av_string(item, "request_key_hash")
            expires_at = _av_string(item, "expires_at")
            sequence = _av_int(item, "sequence")
        except (KeyError, TypeError) as exc:
            raise CloudAuthorityError("invalid_authority") from exc
        if (
            pk != self.pk
            or sk != f"INTENT#{request_key_hash}"
            or kind != "intent"
            or fingerprint != self.dataset_fingerprint
            or stored_hash != request_key_hash
            or not _SHA256.fullmatch(stored_hash)
            or not _canonical_expiry(expires_at)
            or sequence < 1
            or sequence > _MAX_INTENTS
        ):
            raise CloudAuthorityError("invalid_authority")
        return DeletionIntent(stored_hash, expires_at, sequence)

    def append(self, request_key_hash: str, expires_at: str) -> int:
        """Append an immutable intent; matching retries return its sequence.

        A retry with the same hash but a different expiry is invalid. On an
        ambiguous transaction result, a strongly consistent point read and
        bounded snapshot must prove the committed intent before returning.
        """
        if not isinstance(request_key_hash, str) or not _SHA256.fullmatch(request_key_hash):
            raise CloudAuthorityError("invalid_record")
        if not _canonical_expiry(expires_at):
            raise CloudAuthorityError("invalid_record")
        control = self._read_control()
        prior = self._read_intent(request_key_hash)
        if prior is not None:
            if prior.expires_at != expires_at:
                raise CloudAuthorityError("intent_conflict")
            intents, highwater = self.snapshot()
            if highwater < prior.sequence or not any(i == prior for i in intents):
                raise CloudAuthorityError("invalid_authority")
            return prior.sequence
        _, observed_highwater = self.snapshot()
        if observed_highwater != control["highwater"]:
            raise CloudAuthorityError("snapshot_changed")
        if control["highwater"] >= _MAX_INTENTS:
            raise CloudAuthorityError("authority_capacity")

        sequence = control["highwater"] + 1
        if self.writer is not None:
            writer_error = None
            try:
                returned_sequence = self.writer.append(
                    self.dataset_fingerprint,
                    self.owner_instance_id,
                    self.epoch,
                    request_key_hash,
                    expires_at,
                )
            except CloudAuthorityError as exc:
                writer_error = exc
                returned_sequence = None
            except Exception:
                writer_error = CloudAuthorityError("authority_write_failed")
                returned_sequence = None

            # The invocation result alone is not authoritative. Confirm both
            # the pinned control record and the exact immutable intent strongly.
            self._read_control()
            committed = self._read_intent(request_key_hash)
            if committed is not None:
                if committed.expires_at != expires_at:
                    raise CloudAuthorityError("intent_conflict")
                intents, highwater = self.snapshot()
                if (
                    committed.sequence >= sequence
                    and highwater >= committed.sequence
                    and committed in intents
                    and (
                        returned_sequence is None
                        or returned_sequence == committed.sequence
                    )
                ):
                    return committed.sequence
                raise CloudAuthorityError("invalid_authority")
            if writer_error is not None:
                if writer_error.code in {"transaction_conflict", "intent_conflict"}:
                    raise writer_error
                raise CloudAuthorityError("authority_write_failed") from writer_error
            raise CloudAuthorityError("invalid_authority")

        intent = {
            **self._key(f"INTENT#{request_key_hash}"),
            "kind": _s("intent"),
            "dataset_fingerprint": _s(self.dataset_fingerprint),
            "request_key_hash": _s(request_key_hash),
            "expires_at": _s(expires_at),
            "sequence": _n(sequence),
        }
        transaction = [
            {
                "Put": {
                    "TableName": self.table_name,
                    "Item": intent,
                    "ConditionExpression": "attribute_not_exists(#pk)",
                    "ExpressionAttributeNames": {"#pk": "pk"},
                }
            },
            {
                "Update": {
                    "TableName": self.table_name,
                    "Key": self._key(_CONTROL_SK),
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
                        ":owner": _s(self.owner_instance_id),
                        ":epoch": _n(self.epoch),
                        ":current": _n(control["highwater"]),
                        ":next": _n(sequence),
                        ":fingerprint": _s(self.dataset_fingerprint),
                        ":control": _s("control"),
                    },
                }
            },
        ]
        try:
            self.client.transact_write_items(TransactItems=transaction)
            return sequence
        except Exception as exc:
            # A timeout may arrive after DynamoDB committed. Resolve only from
            # strongly consistent evidence; any read uncertainty stays closed.
            try:
                self._read_control()
                committed = self._read_intent(request_key_hash)
                if committed is not None:
                    if committed.expires_at != expires_at:
                        raise CloudAuthorityError("intent_conflict")
                    intents, highwater = self.snapshot()
                    if highwater >= sequence and committed in intents:
                        return committed.sequence
                    raise CloudAuthorityError("invalid_authority")
            except CloudAuthorityError as read_error:
                if read_error.code not in {"authority_unavailable"}:
                    raise
                raise CloudAuthorityError("authority_unavailable") from exc
            response = getattr(exc, "response", None)
            error_code = (
                response.get("Error", {}).get("Code")
                if isinstance(response, dict)
                else None
            )
            if error_code in {"TransactionCanceledException", "ConditionalCheckFailedException"}:
                raise CloudAuthorityError("transaction_conflict") from exc
            raise CloudAuthorityError("authority_unavailable") from exc

    def snapshot(self) -> tuple[list[DeletionIntent], int]:
        """Read and validate a stable, bounded snapshot of all deletion intents."""
        before = self._read_control()
        items: list[dict[str, Any]] = []
        start_key = None
        while True:
            request: dict[str, Any] = {
                "TableName": self.table_name,
                "KeyConditionExpression": "#pk = :pk AND begins_with(#sk, :prefix)",
                "ExpressionAttributeNames": {"#pk": "pk", "#sk": "sk"},
                "ExpressionAttributeValues": {":pk": _s(self.pk), ":prefix": _s("INTENT#")},
                "ConsistentRead": True,
                "Limit": _MAX_INTENTS + 1 - len(items),
            }
            if start_key is not None:
                request["ExclusiveStartKey"] = start_key
            try:
                response = self.client.query(**request)
            except Exception as exc:
                raise CloudAuthorityError("authority_unavailable") from exc
            if type(response) is not dict or type(response.get("Items")) is not list:
                raise CloudAuthorityError("invalid_authority")
            items.extend(response["Items"])
            if len(items) > _MAX_INTENTS:
                raise CloudAuthorityError("authority_capacity")
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                break
            if type(start_key) is not dict:
                raise CloudAuthorityError("invalid_authority")
        intents = [self._intent_from_item(item) for item in items]
        after = self._read_control()
        if before["epoch"] != after["epoch"] or before["highwater"] != after["highwater"]:
            raise CloudAuthorityError("snapshot_changed")
        highwater = before["highwater"]
        sequences = [intent.sequence for intent in intents]
        if (
            len(intents) != highwater
            or sorted(sequences) != list(range(1, highwater + 1))
            or len(set(i.request_key_hash for i in intents)) != len(intents)
        ):
            raise CloudAuthorityError("invalid_authority")
        return sorted(intents, key=lambda intent: intent.sequence), highwater

    def _intent_from_item(self, item: dict[str, Any]) -> DeletionIntent:
        if type(item) is not dict:
            raise CloudAuthorityError("invalid_authority")
        try:
            pk = _av_string(item, "pk")
            sk = _av_string(item, "sk")
            kind = _av_string(item, "kind")
            fingerprint = _av_string(item, "dataset_fingerprint")
            request_hash = _av_string(item, "request_key_hash")
            expires_at = _av_string(item, "expires_at")
            sequence = _av_int(item, "sequence")
        except (KeyError, TypeError) as exc:
            raise CloudAuthorityError("invalid_authority") from exc
        if (
            pk != self.pk
            or not sk.startswith("INTENT#")
            or kind != "intent"
            or fingerprint != self.dataset_fingerprint
            or sk != f"INTENT#{request_hash}"
            or not _SHA256.fullmatch(request_hash)
            or not _canonical_expiry(expires_at)
            or sequence < 1
            or sequence > _MAX_INTENTS
        ):
            raise CloudAuthorityError("invalid_authority")
        return DeletionIntent(request_hash, expires_at, sequence)
