"""Operator-only control bootstrap and physical-instance-fenced replacement.

The EC2 application role cannot perform these writes. Use the approved SSO
operator profile; never put its credentials on the rehearsal instance.
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Any


class OperatorError(RuntimeError):
    pass


def _key(fingerprint: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise OperatorError("invalid_fingerprint")
    return {"pk": {"S": f"DATASET#{fingerprint}"}, "sk": {"S": "CONTROL"}}


def _instance_state(ec2: Any, instance_id: str) -> str:
    if not re.fullmatch(r"i-[0-9a-f]{17}", instance_id):
        raise OperatorError("invalid_instance")
    result = ec2.describe_instances(InstanceIds=[instance_id])
    instances = [i for r in result["Reservations"] for i in r["Instances"]]
    if len(instances) != 1 or instances[0]["InstanceId"] != instance_id:
        raise OperatorError("instance_state_unverified")
    return instances[0]["State"]["Name"]


def initialize_control(ddb, ec2, table: str, fingerprint: str, instance_id: str) -> dict:
    key = _key(fingerprint)
    if _instance_state(ec2, instance_id) != "running":
        raise OperatorError("new_instance_not_running")
    rows = ddb.query(
        TableName=table,
        ConsistentRead=True,
        KeyConditionExpression="#pk = :pk",
        ExpressionAttributeNames={"#pk": "pk"},
        ExpressionAttributeValues={":pk": key["pk"]},
        Limit=1,
    )
    if rows.get("Items"):
        raise OperatorError("authority_already_exists")
    item = {
        **key,
        "kind": {"S": "control"},
        "dataset_fingerprint": {"S": fingerprint},
        "owner_instance_id": {"S": instance_id},
        "epoch": {"N": "1"},
        "highwater": {"N": "0"},
    }
    ddb.put_item(
        TableName=table,
        Item=item,
        ConditionExpression="attribute_not_exists(#pk)",
        ExpressionAttributeNames={"#pk": "pk"},
    )
    return {"operation": "initialize", "owner_instance_id": instance_id, "epoch": 1}


def takeover_control(ddb, ec2, table: str, fingerprint: str, instance_id: str) -> dict:
    from app.cloud_deletion_authority import CloudDeletionAuthority

    key = _key(fingerprint)
    item = ddb.get_item(TableName=table, Key=key, ConsistentRead=True).get("Item")
    if not item:
        raise OperatorError("authority_missing")
    old_owner = item["owner_instance_id"]["S"]
    authority = CloudDeletionAuthority(ddb, table, fingerprint, old_owner)
    _, highwater = authority.snapshot()
    if old_owner == instance_id:
        raise OperatorError("same_owner")
    # A paused process, stopped VM or lost session is insufficient evidence.
    if _instance_state(ec2, old_owner) != "terminated":
        raise OperatorError("old_instance_not_terminated")
    if _instance_state(ec2, instance_id) != "running":
        raise OperatorError("new_instance_not_running")
    ddb.update_item(
        TableName=table,
        Key=key,
        UpdateExpression="SET #owner = :new, #epoch = :next",
        ConditionExpression=(
            "#owner = :old AND #epoch = :epoch AND #highwater = :highwater "
            "AND #fingerprint = :fingerprint AND #kind = :control"
        ),
        ExpressionAttributeNames={
            "#owner": "owner_instance_id",
            "#epoch": "epoch",
            "#highwater": "highwater",
            "#fingerprint": "dataset_fingerprint",
            "#kind": "kind",
        },
        ExpressionAttributeValues={
            ":old": {"S": old_owner},
            ":new": {"S": instance_id},
            ":epoch": {"N": str(authority.epoch)},
            ":next": {"N": str(authority.epoch + 1)},
            ":highwater": {"N": str(highwater)},
            ":fingerprint": {"S": fingerprint},
            ":control": {"S": "control"},
        },
    )
    updated = CloudDeletionAuthority(ddb, table, fingerprint, instance_id)
    _, after = updated.snapshot()
    if updated.epoch != authority.epoch + 1 or after != highwater:
        raise OperatorError("takeover_unverified")
    return {
        "operation": "takeover",
        "old_owner": old_owner,
        "old_state": "terminated",
        "new_owner": instance_id,
        "epoch": updated.epoch,
        "highwater": after,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("initialize", "takeover"))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--instance", required=True)
    args = parser.parse_args(argv)
    import boto3
    from botocore.config import Config

    session = boto3.Session(profile_name=args.profile, region_name="ap-northeast-2")
    options = Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 1})
    try:
        action = initialize_control if args.command == "initialize" else takeover_control
        result = action(
            session.client("dynamodb", config=options),
            session.client("ec2", config=options),
            args.table,
            args.fingerprint,
            args.instance,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, OperatorError) else "operator_operation_unverified"
        print(json.dumps({"error": code}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
