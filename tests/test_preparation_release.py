"""Offline source-evidence release checks for the finance preparation catalog."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path

import pytest

from app import preparation_release, preparation_service
from app.preparation_release import ReleaseInputError, build_release

_REAL_CATALOG = Path(__file__).parents[1] / "app/resources/finance-guides-v1.json"


@pytest.fixture(autouse=True)
def fixed_review_clock(monkeypatch):
    # Fixture bytes represent this review date, regardless of the CI wall clock.
    monkeypatch.setattr(preparation_service, "_today", lambda: date(2026, 10, 20))
    monkeypatch.setattr(preparation_release, "_today", lambda: date(2026, 10, 20))


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def evidence_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, bytes], dict]:
    input_dir = tmp_path / "inputs"
    evidence_dir = input_dir / "evidence"
    evidence_dir.mkdir(parents=True)
    catalog = json.loads(_REAL_CATALOG.read_text(encoding="utf-8"))
    captures: dict[str, bytes] = {}
    records = []
    for index, source in enumerate(catalog["sources"]):
        content = f"synthetic source capture {index}".encode()
        captures[source["id"]] = content
        source["retrieved_body_sha256"] = _digest(content)
        capture_path = f"capture-{index}.bin"
        (evidence_dir / capture_path).write_bytes(content)
        records.append(
            {
                "id": source["id"],
                "url": source["url"],
                "capture_path": capture_path,
                "checked_on": source["checked_on"],
                "review_kind": "official_page_check",
                "reviewer_reference": f"operator-record-{index}",
            }
        )
    catalog_path = input_dir / "catalog.json"
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    manifest = {
        "schema_version": "preparation-source-evidence.v1",
        "catalog_sha256": preparation_service._catalog_fingerprint(catalog),
        "sources": records,
    }
    manifest_path = evidence_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return catalog_path, manifest_path, captures, manifest


def test_complete_evidence_creates_hashed_bundle_and_completion_marker(tmp_path):
    catalog, manifest, captures, _ = evidence_fixture(tmp_path)
    report = tmp_path / "out/report.json"
    bundle = tmp_path / "out/bundle"

    code, result = build_release(catalog, manifest, report, bundle)

    assert code == 0
    assert result["status"] == "complete"
    assert result["readiness_scope"] == "internal_source_bundle"
    assert result["public_release_approval"] == "not_established"
    marker = json.loads((bundle / "completion.json").read_text())
    assert marker["status"] == "complete"
    if os.name == "posix":
        assert bundle.stat().st_mode & 0o777 == 0o700
    for relative, expected_hash in marker["files_sha256"].items():
        assert _digest((bundle / relative).read_bytes()) == expected_hash
    evidence_manifest = json.loads((bundle / "evidence-manifest.json").read_text())
    assert evidence_manifest["schema_version"] == "preparation-source-evidence.v1"
    assert {s["id"] for s in evidence_manifest["sources"]} == set(captures)
    for index, content in enumerate(captures.values()):
        assert (bundle / f"captures/source-{index:03d}.bin").read_bytes() == content
    assert all(
        source["reviewer_reference"] not in report.read_text()
        for source in evidence_manifest["sources"]
    )

    audit_code, audit = build_release(
        bundle / "catalog.json",
        bundle / "evidence-manifest.json",
        tmp_path / "out/re-audit.json",
    )
    assert audit_code == 0
    assert audit["status"] == "complete"

    first_capture = bundle / evidence_manifest["sources"][0]["capture_path"]
    first_capture.write_bytes(b"tampered after release")
    tamper_code, tamper_report = build_release(
        bundle / "catalog.json",
        bundle / "evidence-manifest.json",
        tmp_path / "out/tamper-audit.json",
    )
    assert tamper_code == 2
    assert "capture_digest_mismatch" in tamper_report["issue_codes"]


def test_current_catalog_without_retained_captures_is_blocked(tmp_path):
    report = tmp_path / "report.json"

    code, result = build_release(_REAL_CATALOG, None, report)

    assert code == 2
    assert result["status"] == "blocked"
    assert "missing_evidence_manifest" in result["issue_codes"]


@pytest.mark.parametrize(
    ("today", "expected_exit"), [(date(2026, 11, 19), 0), (date(2026, 11, 20), 1)]
)
def test_unchanged_evidence_can_expire_without_being_tampered(tmp_path, monkeypatch,
                                                           today, expected_exit):
    catalog, manifest, _, _ = evidence_fixture(tmp_path)
    monkeypatch.setattr(preparation_service, "_today", lambda: today)
    monkeypatch.setattr(preparation_release, "_today", lambda: today)
    code, report = build_release(catalog, manifest, tmp_path / "time-check.json")
    assert code == expected_exit
    if expected_exit:
        assert report["status"] == "error"
        assert report["issue_codes"] == ["invalid_catalog"]
    else:
        assert report["status"] == "complete"


def test_tampered_capture_and_catalog_digest_are_blocked(tmp_path):
    catalog, manifest, _, data = evidence_fixture(tmp_path)
    data["sources"][0]["capture_path"]
    capture = manifest.parent / data["sources"][0]["capture_path"]
    capture.write_bytes(b"tampered")
    report = tmp_path / "report.json"

    code, result = build_release(catalog, manifest, report)

    assert code == 2
    assert "capture_digest_mismatch" in result["issue_codes"]

    other = tmp_path / "other"
    other.mkdir()
    changed_catalog = json.loads(catalog.read_text())
    changed_catalog["facts"][0]["label"] += " changed"
    changed_path = other / "catalog.json"
    changed_path.write_text(json.dumps(changed_catalog, ensure_ascii=False))
    # The original manifest is outside the changed catalog's directory by design.
    report2 = tmp_path / "report2.json"
    code, result = build_release(changed_path, manifest, report2)
    assert code == 2
    assert "catalog_digest_mismatch" in result["issue_codes"]


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda rows: rows.pop(), "missing_source_metadata"),
        (lambda rows: rows.append(dict(rows[0])), "duplicate_source"),
        (
            lambda rows: rows.append(
                {**rows[0], "id": "unknown-source", "capture_path": rows[0]["capture_path"]}
            ),
            "unknown_source",
        ),
        (lambda rows: rows[0].update(url="https://example.invalid/"), "source_url_mismatch"),
        (lambda rows: rows[0].update(checked_on="2999-01-01"), "future_review_date"),
        (lambda rows: rows[0].update(reviewer_reference=" "), "missing_reviewer_reference"),
    ],
)
def test_manifest_metadata_issues_block_release(tmp_path, mutation, code):
    catalog, manifest_path, _, manifest = evidence_fixture(tmp_path)
    mutation(manifest["sources"])
    manifest_path.write_text(json.dumps(manifest))

    exit_code, result = build_release(catalog, manifest_path, tmp_path / "report.json")

    assert exit_code == 2
    assert code in result["issue_codes"]
    assert not (tmp_path / "bundle").exists()


def test_source_count_ceiling_blocks_manifest(tmp_path):
    catalog, manifest_path, _, manifest = evidence_fixture(tmp_path)
    manifest["sources"] *= 9
    manifest_path.write_text(json.dumps(manifest))

    exit_code, result = build_release(catalog, manifest_path, tmp_path / "report.json")

    assert exit_code == 2
    assert "too_many_sources" in result["issue_codes"]


def test_aggregate_capture_ceiling_blocks_manifest(tmp_path):
    catalog_path, manifest_path, _, manifest = evidence_fixture(tmp_path)
    catalog_data = json.loads(catalog_path.read_text())
    contents = b"x" * (3 * 1024 * 1024)
    for index, source in enumerate(catalog_data["sources"]):
        capture_path = manifest["sources"][index]["capture_path"]
        (manifest_path.parent / capture_path).write_bytes(contents)
        source["retrieved_body_sha256"] = _digest(contents)
    catalog_path.write_text(json.dumps(catalog_data, ensure_ascii=False))
    manifest["catalog_sha256"] = preparation_service._catalog_fingerprint(catalog_data)
    manifest_path.write_text(json.dumps(manifest))

    exit_code, result = build_release(
        catalog_path, manifest_path, tmp_path / "aggregate-report.json"
    )

    assert exit_code == 2
    assert "aggregate_capture_too_large" in result["issue_codes"]


def test_capture_path_escape_and_symlink_are_rejected(tmp_path):
    catalog, manifest_path, _, manifest = evidence_fixture(tmp_path)
    manifest["sources"][0]["capture_path"] = "../outside.bin"
    manifest_path.write_text(json.dumps(manifest))
    code, result = build_release(catalog, manifest_path, tmp_path / "escape-report.json")
    assert code == 2
    assert "invalid_capture_path" in result["issue_codes"]

    catalog, manifest_path, _, manifest_data = evidence_fixture(tmp_path / "symlink")
    target = manifest_path.parent / "real.bin"
    target.write_bytes(b"target")
    (manifest_path.parent / "linked.bin").symlink_to(target)
    manifest_data["sources"][0]["capture_path"] = "linked.bin"
    manifest_path.write_text(json.dumps(manifest_data))
    code, result = build_release(catalog, manifest_path, tmp_path / "symlink-report.json")
    assert code == 2
    assert "invalid_capture_path" in result["issue_codes"]


def test_output_collision_and_overlap_are_rejected(tmp_path):
    catalog, manifest, _, _ = evidence_fixture(tmp_path)
    report = tmp_path / "collision.json"
    report.write_text("keep")
    with pytest.raises(ReleaseInputError) as error:
        build_release(catalog, manifest, report)
    assert error.value.code == "output_collision"
    assert report.read_text() == "keep"

    with pytest.raises(ReleaseInputError) as error:
        build_release(catalog, manifest, manifest.parent / "report.json")
    assert error.value.code == "output_overlaps_inputs"

    nested_bundle = tmp_path / "nested-bundle"
    nested_report = nested_bundle / "report.json"
    with pytest.raises(ReleaseInputError) as error:
        build_release(catalog, manifest, nested_report, nested_bundle)
    assert error.value.code == "output_collision"


def test_existing_report_collision_does_not_create_bundle(tmp_path):
    catalog, manifest, _, _ = evidence_fixture(tmp_path)
    report = tmp_path / "already-there.json"
    report.write_text("preserve this report")
    bundle = tmp_path / "new-bundle"

    with pytest.raises(ReleaseInputError) as error:
        build_release(catalog, manifest, report, bundle)

    assert error.value.code == "output_collision"
    assert report.read_text() == "preserve this report"
    assert not bundle.exists()


def test_default_catalog_validation_and_card_behavior_remain_available():
    assert preparation_service.catalog_readiness()["status"] == "ready"
    result = preparation_service.build_card(
        {"issue": "repayment", "selected_facts": ["payment_due", "not_overdue"]}
    )
    assert result["status"] == "ready"
