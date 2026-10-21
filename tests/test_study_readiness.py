from __future__ import annotations

import hashlib
import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from app import preparation_service
from app.study_readiness import ROLES, audit_plan, main


def _write_json(path: Path, value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _canonical_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _complete_fixture(tmp_path: Path) -> tuple[Path, dict[str, Path], list[str]]:
    plan_dir = tmp_path / "plan"
    inputs_dir = plan_dir / "auditinputs"
    inputs_dir.mkdir(parents=True)
    case_ids = [f"case-{number}" for number in range(1, 7)]
    catalog_source = Path(__file__).parents[1] / "app/resources/finance-guides-v1.json"
    shutil.copyfile(catalog_source, inputs_dir / "catalog.json")
    catalog_value = json.loads((inputs_dir / "catalog.json").read_text(encoding="utf-8"))
    catalog_hash = _canonical_hash(catalog_value)
    _write_json(
        inputs_dir / "scenarios.json",
        {"catalog_sha256": catalog_hash, "case_ids": case_ids},
    )
    protocol_hash = _write_json(
        inputs_dir / "protocol.json",
        {"status": "approved", "catalog_sha256": catalog_hash, "case_ids": case_ids},
    )
    _write_json(
        inputs_dir / "source-audit.json",
        {
            "schema_version": "preparation-release-report.v1",
            "status": "complete",
            "catalog_sha256": catalog_hash,
            "readiness_scope": "internal_source_bundle",
            "public_release_approval": "not_established",
            "participant_research_approval": "not_established",
        },
    )
    for name in ("professional-keys", "professional-review"):
        value = {
            "status": "approved",
            "catalog_sha256": catalog_hash,
            "protocol_sha256": protocol_hash,
            "case_ids": case_ids,
        }
        if name == "professional-review":
            value.update(
                independent_human_reviewer=True,
                reviewer_actor="EXTERNAL_REVIEWER_PRIVATE_NAME",
                external_record_ref="EXTERNAL_REVIEW_RECORD_PRIVATE_REF",
            )
        else:
            value["keys"] = [
                {"case_id": case_id, "key": f"fictional key for {case_id}"}
                for case_id in case_ids
            ]
        _write_json(inputs_dir / f"{name}.json", value)
    _write_json(
        inputs_dir / "raters.json",
        {
            "raters": [
                {
                    "slot": "A",
                    "declared_id": "rater-a",
                    "role": "independent reviewer",
                    "independent_human": True,
                    "evidence_ref": "record-a",
                    "evidence_sha256": "a" * 64,
                },
                {
                    "slot": "B",
                    "declared_id": "rater-b",
                    "role": "independent reviewer",
                    "independent_human": True,
                    "evidence_ref": "record-b",
                    "evidence_sha256": "b" * 64,
                },
            ]
        },
    )
    _write_json(
        inputs_dir / "consent.json",
        {
            "status": "approved",
            "contact": "CONFIDENTIAL_CONTACT_VALUE",
            "withdrawal": "CONFIDENTIAL_WITHDRAWAL_TEXT",
            "retention": "90 days after collection",
            "compensation": "No compensation",
            "publication": "Anonymous aggregate only",
        },
    )
    _write_json(
        inputs_dir / "operator.json",
        {
            "status": "approved",
            "storage": "Restricted encrypted project folder",
            "deletion_owner": "CONFIDENTIAL_OPERATOR_NAME",
        },
    )
    for role in ("moderator-observer", "comparator-preflight"):
        _write_json(
            inputs_dir / f"{role}.json",
            {
                "status": "complete",
                "human_present": True,
                "researcher_only_walkthrough": False,
                "performed_by_role": "independent observer",
                "external_record_ref": f"{role}-record",
            },
        )
    role_files = {
        "current_catalog": "catalog.json",
        "six_scenarios": "scenarios.json",
        "protocol": "protocol.json",
        "source_audit": "source-audit.json",
        "professional_keys": "professional-keys.json",
        "professional_review": "professional-review.json",
        "rater_roster": "raters.json",
        "consent_notice": "consent.json",
        "operator_policy": "operator.json",
        "moderator_observer_rehearsal": "moderator-observer.json",
        "comparator_preflight": "comparator-preflight.json",
    }
    inputs = {}
    for role, filename in role_files.items():
        path = inputs_dir / filename
        inputs[role] = {
            "root": "plan",
            "path": f"auditinputs/{filename}",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    plan = plan_dir / "plan.json"
    plan.write_text(
        json.dumps(
            {"schema_version": 1, "expected_catalog_sha256": catalog_hash, "inputs": inputs}
        ),
        encoding="utf-8",
    )
    return plan, {role: inputs_dir / filename for role, filename in role_files.items()}, case_ids


@pytest.fixture(autouse=True)
def fixed_review_clock(monkeypatch):
    monkeypatch.setattr(preparation_service, "_today", lambda: date(2026, 10, 20))


def _rewrite_plan(plan_path: Path, mutate) -> None:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    mutate(plan)
    plan_path.write_text(json.dumps(plan), encoding="utf-8")


def test_all_synthetic_records_only_reach_operator_review(tmp_path: Path):
    plan, _, _ = _complete_fixture(tmp_path)

    report = audit_plan(plan)

    assert report["status"] == "ready_for_operator_review"
    assert report["participant_sessions_admitted"] is False
    assert report["public_release_approved"] is False
    assert report["authenticity_verified"] is False
    assert report["operator_must_verify_records_and_consent_at_session_time"] is True


def test_missing_current_prerequisites_are_blocked(tmp_path: Path):
    plan, _, _ = _complete_fixture(tmp_path)
    _rewrite_plan(plan, lambda value: value["inputs"].update(
        {"professional_review": None, "rater_roster": None, "comparator_preflight": None}
    ))

    report = audit_plan(plan)

    assert report["status"] == "blocked"
    assert {issue["role"] for issue in report["issues"]} >= {
        "professional_review", "rater_roster", "comparator_preflight"
    }


def test_boolean_only_consent_and_operator_flags_do_not_pass(tmp_path: Path):
    plan, files, _ = _complete_fixture(tmp_path)
    _write_json(files["consent_notice"], {"status": "approved", "consent_notice_confirmed": True})
    _write_json(files["operator_policy"], {"status": "approved", "retention_confirmed": True})
    _rewrite_plan(plan, lambda value: [
        value["inputs"][role].update(sha256=hashlib.sha256(files[role].read_bytes()).hexdigest())
        for role in ("consent_notice", "operator_policy")
    ])

    report = audit_plan(plan)

    assert report["status"] == "blocked"
    assert any(issue["code"] == "explicit_policy_text_incomplete" for issue in report["issues"])


def test_changed_artifact_bytes_are_stale_and_blocked(tmp_path: Path):
    plan, files, _ = _complete_fixture(tmp_path)
    files["current_catalog"].write_text('{"catalog":"changed"}', encoding="utf-8")

    report = audit_plan(plan)

    assert report["status"] == "blocked"
    assert {issue["code"] for issue in report["issues"]} >= {"artifact_hash_mismatch"}


def test_expired_catalog_is_blocked_even_when_hashes_are_updated(tmp_path: Path):
    plan, files, _ = _complete_fixture(tmp_path)
    catalog = json.loads(files["current_catalog"].read_text(encoding="utf-8"))
    catalog["checked_on"] = "2026-09-01"
    for source in catalog["sources"]:
        source["checked_on"] = "2026-09-01"
    _write_json(files["current_catalog"], catalog)
    new_catalog_hash = _canonical_hash(catalog)
    raw_catalog_hash = hashlib.sha256(files["current_catalog"].read_bytes()).hexdigest()
    plan_value = json.loads(plan.read_text(encoding="utf-8"))
    plan_value["expected_catalog_sha256"] = new_catalog_hash
    plan_value["inputs"]["current_catalog"]["sha256"] = raw_catalog_hash
    plan.write_text(json.dumps(plan_value), encoding="utf-8")

    report = audit_plan(plan)

    assert report["status"] == "blocked"
    assert {issue["code"] for issue in report["issues"]} >= {"catalog_not_ready"}


def test_duplicate_or_copied_rater_record_does_not_pass(tmp_path: Path):
    plan, files, _ = _complete_fixture(tmp_path)
    copied = json.loads(files["rater_roster"].read_text())
    copied["raters"][1]["evidence_ref"] = copied["raters"][0]["evidence_ref"]
    copied["raters"][1]["evidence_sha256"] = copied["raters"][0]["evidence_sha256"]
    _write_json(files["rater_roster"], copied)
    _rewrite_plan(plan, lambda value: value["inputs"]["rater_roster"].update(
        sha256=hashlib.sha256(files["rater_roster"].read_bytes()).hexdigest()
    ))

    report = audit_plan(plan)

    assert report["status"] == "blocked"
    assert any(
        issue["code"] == "two_distinct_human_rater_records_required"
        for issue in report["issues"]
    )


@pytest.mark.parametrize("bad_slot", [[], {}])
def test_unhashable_rater_slot_is_blocked_without_traceback(tmp_path: Path, bad_slot):
    plan, files, _ = _complete_fixture(tmp_path)
    roster = json.loads(files["rater_roster"].read_text())
    roster["raters"][0]["slot"] = bad_slot
    _write_json(files["rater_roster"], roster)
    _rewrite_plan(
        plan,
        lambda value: value["inputs"]["rater_roster"].update(
            sha256=hashlib.sha256(files["rater_roster"].read_bytes()).hexdigest()
        ),
    )

    report = audit_plan(plan)

    assert report["status"] == "blocked"
    assert any(
        issue["code"] == "two_distinct_human_rater_records_required"
        for issue in report["issues"]
    )


@pytest.mark.parametrize("role,field,value", [
    ("six_scenarios", "case_ids", ["case-1", "case-2", "case-3", "case-4", "case-5", "other"]),
    ("professional_review", "catalog_sha256", "0" * 64),
    ("professional_keys", "protocol_sha256", "0" * 64),
    ("professional_review", "case_ids", ["wrong"] * 6),
])
def test_wrong_case_protocol_or_catalog_binding_blocks(tmp_path: Path, role, field, value):
    plan, files, _ = _complete_fixture(tmp_path)
    data = json.loads(files[role].read_text())
    data[field] = value
    _write_json(files[role], data)
    _rewrite_plan(plan, lambda value: value["inputs"][role].update(
        sha256=hashlib.sha256(files[role].read_bytes()).hexdigest()
    ))

    assert audit_plan(plan)["status"] == "blocked"


def test_pending_or_researcher_only_rehearsal_is_blocked(tmp_path: Path):
    plan, files, _ = _complete_fixture(tmp_path)
    rehearsal = json.loads(files["moderator_observer_rehearsal"].read_text())
    rehearsal["researcher_only_walkthrough"] = True
    _write_json(files["moderator_observer_rehearsal"], rehearsal)
    _rewrite_plan(plan, lambda value: value["inputs"]["moderator_observer_rehearsal"].update(
        sha256=hashlib.sha256(files["moderator_observer_rehearsal"].read_bytes()).hexdigest()
    ))

    report = audit_plan(plan)

    assert report["status"] == "blocked"
    assert any(
        issue["code"] == "actual_human_rehearsal_record_required"
        for issue in report["issues"]
    )


def test_traversal_and_symlink_inputs_are_invalid(tmp_path: Path):
    plan, files, _ = _complete_fixture(tmp_path)
    _rewrite_plan(
        plan,
        lambda value: value["inputs"]["current_catalog"].update(path="../outside.json"),
    )
    assert audit_plan(plan)["status"] == "invalid"

    plan, files, _ = _complete_fixture(tmp_path / "second")
    target = files["current_catalog"]
    link = target.parent / "catalog-link.json"
    link.symlink_to(target)
    _rewrite_plan(
        plan,
        lambda value: value["inputs"]["current_catalog"].update(
            path="auditinputs/catalog-link.json"
        ),
    )
    assert audit_plan(plan)["status"] == "invalid"


def test_report_omits_reviewers_policy_text_and_source_strings(tmp_path: Path):
    plan, _, _ = _complete_fixture(tmp_path)
    report = json.dumps(audit_plan(plan), ensure_ascii=False)

    for private_value in (
        "EXTERNAL_REVIEWER_PRIVATE_NAME", "EXTERNAL_REVIEW_RECORD_PRIVATE_REF",
        "CONFIDENTIAL_CONTACT_VALUE", "CONFIDENTIAL_OPERATOR_NAME",
    ):
        assert private_value not in report
    assert set(audit_plan(plan)["roles"]) == set(ROLES)


@pytest.mark.parametrize(
    ("contents", "expected_code"),
    [
        (
            b'{"schema_version":1,"expected_catalog_sha256":NaN,"inputs":{}}',
            "non_finite_json_number",
        ),
        (b"[" * 3000 + b"0" + b"]" * 3000, "invalid_json"),
    ],
)
def test_nonstandard_or_deep_json_returns_sanitized_invalid_report(
    tmp_path: Path, contents: bytes, expected_code: str
):
    plan = tmp_path / "invalid-plan.json"
    plan.write_bytes(contents)

    report = audit_plan(plan)
    serialized = json.dumps(report)

    assert report["status"] == "invalid"
    assert report["issues"] == [{"role": "plan", "code": expected_code}]
    assert "NaN" not in serialized
    assert "expected_catalog_sha256" not in serialized


def test_cli_exclusive_report_and_exit_codes(tmp_path: Path):
    plan, _, _ = _complete_fixture(tmp_path)
    report = tmp_path / "new-report.json"

    assert main(["--plan", str(plan), "--report", str(report)]) == 0
    assert json.loads(report.read_text())["status"] == "ready_for_operator_review"
    assert main(["--plan", str(plan), "--report", str(report)]) == 1

    blocked_plan, _, _ = _complete_fixture(tmp_path / "blocked")
    value = json.loads(blocked_plan.read_text())
    value["inputs"]["rater_roster"] = None
    blocked_plan.write_text(json.dumps(value))
    assert main(
        ["--plan", str(blocked_plan), "--report", str(tmp_path / "blocked-report.json")]
    ) == 2
