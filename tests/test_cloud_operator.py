from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.cloud_rehearsal_operator import OperatorError, initialize_control, takeover_control

_FP = "a" * 64
_OLD = "i-" + "1" * 17
_NEW = "i-" + "2" * 17


class EC2:
    def __init__(self, old_state):
        self.old_state = old_state

    def describe_instances(self, InstanceIds):
        identity = InstanceIds[0]
        state = self.old_state if identity == _OLD else "running"
        return {
            "Reservations": [
                {
                    "Instances": [
                        {"InstanceId": identity, "State": {"Name": state}},
                    ]
                }
            ]
        }


class DDB:
    def __init__(self):
        self.item = None
        self.updates = 0
        self.conflict = False

    def query(self, **kwargs):
        if ":prefix" in kwargs["ExpressionAttributeValues"]:
            return {"Items": []}
        return {"Items": [] if self.item is None else [deepcopy(self.item)]}

    def put_item(self, **kwargs):
        assert kwargs["ConditionExpression"] == "attribute_not_exists(#pk)"
        assert self.item is None
        self.item = deepcopy(kwargs["Item"])

    def get_item(self, **kwargs):
        assert kwargs["ConsistentRead"] is True
        return {} if self.item is None else {"Item": deepcopy(self.item)}

    def update_item(self, **kwargs):
        self.updates += 1
        if self.conflict:
            raise RuntimeError("conditional conflict")
        values = kwargs["ExpressionAttributeValues"]
        assert self.item["owner_instance_id"] == values[":old"]
        assert self.item["epoch"] == values[":epoch"]
        assert self.item["highwater"] == values[":highwater"]
        assert self.item["dataset_fingerprint"] == values[":fingerprint"]
        assert self.item["kind"] == values[":control"]
        self.item["owner_instance_id"] = values[":new"]
        self.item["epoch"] = values[":next"]


@pytest.mark.parametrize("old_state", ["running", "stopped", "stopping", "shutting-down"])
def test_takeover_never_accepts_merely_stopped_or_unreachable_owner(old_state):
    ddb = DDB()
    initialize_control(ddb, EC2("running"), "test", _FP, _OLD)
    before = deepcopy(ddb.item)
    with pytest.raises(OperatorError, match="old_instance_not_terminated"):
        takeover_control(ddb, EC2(old_state), "test", _FP, _NEW)
    assert ddb.item == before
    assert ddb.updates == 0


def test_takeover_after_termination_keeps_highwater_and_advances_epoch():
    ddb = DDB()
    initialize_control(ddb, EC2("running"), "test", _FP, _OLD)
    result = takeover_control(ddb, EC2("terminated"), "test", _FP, _NEW)
    assert result["old_state"] == "terminated"
    assert result["epoch"] == 2
    assert result["highwater"] == 0
    assert ddb.item["owner_instance_id"] == {"S": _NEW}


def test_bootstrap_refuses_to_reset_existing_partition():
    ddb = DDB()
    initialize_control(ddb, EC2("running"), "test", _FP, _OLD)
    before = deepcopy(ddb.item)
    with pytest.raises(OperatorError, match="already_exists"):
        initialize_control(ddb, EC2("running"), "test", _FP, _NEW)
    assert ddb.item == before


def test_concurrent_control_change_does_not_force_takeover():
    ddb = DDB()
    initialize_control(ddb, EC2("running"), "test", _FP, _OLD)
    before = deepcopy(ddb.item)
    ddb.conflict = True
    with pytest.raises(RuntimeError, match="conditional conflict"):
        takeover_control(ddb, EC2("terminated"), "test", _FP, _NEW)
    assert ddb.item == before
