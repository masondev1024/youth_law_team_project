"""Content-free, local audit for human study-preparation evidence.

This is a preparation gate only. A passing result means ``ready_for_operator_review``;
it never approves recruitment, participant sessions, publication, or legal validity.

Plan shape (all role values may be null while evidence is pending)::

    {"schema_version": 1, "expected_catalog_sha256": "<sha256>",
     "inputs": {"current_catalog": null, ...}}

Each non-null role is ``{"root": "plan", "path": "auditinputs/file.json",
"sha256": "<sha256>"}``. ``plan`` is the plan file's directory; other root names
must be explicitly passed as ``--root NAME=/absolute/directory``. Evidence JSON
uses the explicit contracts checked below. Hashes bind bytes; they do not prove who
created a record or whether consent/review happened as described.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from app import preparation_service
from app.preparation_models import PreparationError

PLAN_LIMIT = 256 * 1024
INPUT_LIMIT = 4 * 1024 * 1024
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ROLES = (
    "current_catalog",
    "six_scenarios",
    "protocol",
    "source_audit",
    "professional_keys",
    "professional_review",
    "rater_roster",
    "consent_notice",
    "operator_policy",
    "moderator_observer_rehearsal",
    "comparator_preflight",
)
SENSITIVE_POLICY_FIELDS = (
    "contact",
    "withdrawal",
    "storage",
    "deletion_owner",
    "retention",
    "compensation",
    "publication",
)


class AuditInputError(ValueError):
    """A malformed plan or unsafe/unreadable artifact reference."""


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AuditInputError("duplicate_json_key")
        result[key] = value
    return result


def _parse_json(raw: bytes) -> Any:
    def reject_non_finite(_value: str) -> Any:
        raise AuditInputError("non_finite_json_number")

    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=reject_non_finite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise AuditInputError("invalid_json") from exc


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and HASH_RE.fullmatch(value) is not None


def _canonical_catalog_hash(catalog: Any) -> str | None:
    if not isinstance(catalog, dict):
        return None
    canonical = json.dumps(
        catalog, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _contains_symlink(path: Path) -> bool:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _safe_component_path(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise AuditInputError("invalid_relative_path")
    candidate_rel = PurePosixPath(relative)
    if candidate_rel.is_absolute() or any(part in ("", ".", "..") for part in candidate_rel.parts):
        raise AuditInputError("path_outside_root")
    current = root
    for part in candidate_rel.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise AuditInputError("input_unavailable") from exc
        if stat.S_ISLNK(mode):
            raise AuditInputError("symlink_input")
    resolved = current.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve(strict=True)):
        raise AuditInputError("path_outside_root")
    if not resolved.is_file():
        raise AuditInputError("input_not_file")
    return resolved


def _read_bounded(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
    except OSError as exc:
        raise AuditInputError("input_unavailable") from exc
    if len(raw) > limit:
        raise AuditInputError("input_too_large")
    return raw


def _nonempty_string(data: dict[str, Any], field: str) -> bool:
    value = data.get(field)
    return isinstance(value, str) and bool(value.strip())


def _six_unique_strings(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 6
        and all(isinstance(item, str) and item.strip() for item in value)
        and len(set(value)) == 6
    )


def _issue(issues: list[dict[str, str]], role: str, code: str) -> None:
    issues.append({"role": role, "code": code})


def _load_inputs(
    plan: dict[str, Any], plan_path: Path, roots: dict[str, Path]
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    loaded: dict[str, dict[str, Any]] = {}
    root_map = {"plan": plan_path.parent, **roots}
    refs = plan.get("inputs")
    if not isinstance(refs, dict) or set(refs) != set(ROLES):
        raise AuditInputError("input_roles_must_match_schema")
    seen_paths: set[Path] = set()
    for role in ROLES:
        ref = refs[role]
        if ref is None:
            _issue(issues, role, "missing_evidence")
            continue
        if not isinstance(ref, dict) or set(ref) != {"root", "path", "sha256"}:
            raise AuditInputError("invalid_artifact_reference")
        root_name, expected_hash = ref["root"], ref["sha256"]
        if not isinstance(root_name, str) or root_name not in root_map:
            raise AuditInputError("undeclared_root")
        if not _is_hash(expected_hash):
            raise AuditInputError("invalid_artifact_hash")
        path = _safe_component_path(root_map[root_name], ref["path"])
        if path in seen_paths:
            raise AuditInputError("artifact_roles_must_use_unique_files")
        seen_paths.add(path)
        raw = _read_bounded(path, INPUT_LIMIT)
        actual_hash = hashlib.sha256(raw).hexdigest()
        if actual_hash != expected_hash:
            _issue(issues, role, "artifact_hash_mismatch")
            continue
        try:
            data = _parse_json(raw)
        except AuditInputError:
            _issue(issues, role, "invalid_evidence_json")
            continue
        if not isinstance(data, dict):
            _issue(issues, role, "evidence_must_be_object")
            continue
        loaded[role] = {"data": data, "sha256": actual_hash}
        if role == "current_catalog":
            loaded[role]["raw"] = raw
    return loaded, issues


def _check_evidence(
    plan: dict[str, Any], loaded: dict[str, dict[str, Any]], issues: list[dict[str, str]]
) -> None:
    catalog_hash = plan.get("expected_catalog_sha256")
    if not _is_hash(catalog_hash):
        raise AuditInputError("invalid_expected_catalog_hash")

    catalog = loaded.get("current_catalog")
    if catalog:
        canonical_hash = _canonical_catalog_hash(catalog["data"])
        if canonical_hash is None:
            _issue(issues, "current_catalog", "catalog_must_be_json_object")
        elif canonical_hash != catalog_hash:
            _issue(issues, "current_catalog", "catalog_hash_mismatch")
        try:
            preparation_service._load_catalog(raw_bytes=catalog["raw"])
        except PreparationError:
            _issue(issues, "current_catalog", "catalog_not_ready")

    scenarios = loaded.get("six_scenarios", {}).get("data", {})
    case_ids = scenarios.get("case_ids")
    if loaded.get("six_scenarios"):
        if scenarios.get("catalog_sha256") != catalog_hash:
            _issue(issues, "six_scenarios", "catalog_binding_mismatch")
        if not _six_unique_strings(case_ids):
            _issue(issues, "six_scenarios", "case_ids_must_be_six_unique")

    protocol_entry = loaded.get("protocol")
    protocol = protocol_entry["data"] if protocol_entry else {}
    protocol_hash = protocol_entry["sha256"] if protocol_entry else None
    if protocol_entry:
        if protocol.get("catalog_sha256") != catalog_hash:
            _issue(issues, "protocol", "catalog_binding_mismatch")
        if not _six_unique_strings(protocol.get("case_ids")):
            _issue(issues, "protocol", "case_ids_must_be_six_unique")
        elif case_ids and protocol.get("case_ids") != case_ids:
            _issue(issues, "protocol", "case_ids_mismatch")
        if protocol.get("status") != "approved":
            _issue(issues, "protocol", "not_approved")

    source = loaded.get("source_audit", {}).get("data", {})
    if loaded.get("source_audit"):
        if source.get("status") != "complete":
            _issue(issues, "source_audit", "not_complete")
        if source.get("catalog_sha256") != catalog_hash:
            _issue(issues, "source_audit", "catalog_binding_mismatch")
        if (
            source.get("schema_version") != "preparation-release-report.v1"
            or source.get("readiness_scope") != "internal_source_bundle"
            or source.get("public_release_approval") != "not_established"
            or source.get("participant_research_approval") != "not_established"
        ):
            _issue(
                issues,
                "source_audit",
                "scope_must_be_internal_not_legal_or_participant_approval",
            )

    for role in ("professional_keys", "professional_review"):
        entry = loaded.get(role)
        if not entry:
            continue
        data = entry["data"]
        if data.get("status") != "approved":
            _issue(issues, role, "not_approved")
        if data.get("catalog_sha256") != catalog_hash:
            _issue(issues, role, "catalog_binding_mismatch")
        if data.get("protocol_sha256") != protocol_hash:
            _issue(issues, role, "protocol_binding_mismatch")
        if not _six_unique_strings(data.get("case_ids")):
            _issue(issues, role, "case_ids_must_be_six_unique")
        elif case_ids and data.get("case_ids") != case_ids:
            _issue(issues, role, "case_ids_mismatch")
    keys = loaded.get("professional_keys", {}).get("data", {}).get("keys")
    if loaded.get("professional_keys"):
        valid_keys = isinstance(keys, list) and len(keys) == 6
        if valid_keys:
            key_case_ids = [
                key.get("case_id") for key in keys
                if isinstance(key, dict) and _nonempty_string(key, "key")
            ]
            valid_keys = key_case_ids == case_ids
        if not valid_keys:
            _issue(issues, "professional_keys", "six_case_keys_required")
    review = loaded.get("professional_review", {}).get("data", {})
    if loaded.get("professional_review"):
        if review.get("independent_human_reviewer") is not True:
            _issue(issues, "professional_review", "independent_human_not_declared")
        if not _nonempty_string(review, "reviewer_actor") or not _nonempty_string(
            review, "external_record_ref"
        ):
            _issue(issues, "professional_review", "reviewer_record_reference_missing")

    roster = loaded.get("rater_roster", {}).get("data", {})
    if loaded.get("rater_roster"):
        raters = roster.get("raters")
        valid = (
            isinstance(raters, list)
            and len(raters) == 2
            and all(
                isinstance(rater, dict) and isinstance(rater.get("slot"), str)
                for rater in raters
            )
        )
        if valid:
            by_slot = {r["slot"]: r for r in raters}
            valid = set(by_slot) == {"A", "B"}
            if valid:
                a, b = by_slot["A"], by_slot["B"]
                valid = (
                    all(
                        _nonempty_string(r, "declared_id")
                        and _nonempty_string(r, "role")
                        and _nonempty_string(r, "evidence_ref")
                        and _is_hash(r.get("evidence_sha256"))
                        and r.get("independent_human") is True
                        for r in (a, b)
                    )
                    and a["declared_id"] != b["declared_id"]
                    and a["evidence_ref"] != b["evidence_ref"]
                    and a["evidence_sha256"] != b["evidence_sha256"]
                )
        if not valid:
            _issue(issues, "rater_roster", "two_distinct_human_rater_records_required")

    # Explicit human-readable policy fields are required; booleans alone never pass.
    for role in ("consent_notice", "operator_policy"):
        entry = loaded.get(role)
        if not entry:
            continue
        data = entry["data"]
        if data.get("status") != "approved":
            _issue(issues, role, "not_approved")
    combined = {
        field: any(
            _nonempty_string(loaded.get(role, {}).get("data", {}), field)
            for role in ("consent_notice", "operator_policy")
        )
        for field in SENSITIVE_POLICY_FIELDS
    }
    if not all(combined.values()):
        for role in ("consent_notice", "operator_policy"):
            _issue(issues, role, "explicit_policy_text_incomplete")

    for role in ("moderator_observer_rehearsal", "comparator_preflight"):
        entry = loaded.get(role)
        if not entry:
            continue
        data = entry["data"]
        if (
            data.get("status") != "complete"
            or data.get("human_present") is not True
            or data.get("researcher_only_walkthrough") is not False
            or not _nonempty_string(data, "performed_by_role")
            or not _nonempty_string(data, "external_record_ref")
        ):
            _issue(issues, role, "actual_human_rehearsal_record_required")


def audit_plan(plan_path: Path, roots: dict[str, Path] | None = None) -> dict[str, Any]:
    """Audit a bounded plan and return a content-free report object."""
    roots = roots or {}
    try:
        plan_path = plan_path.expanduser().absolute()
        if _contains_symlink(plan_path):
            raise AuditInputError("symlink_plan")
        raw_plan = _read_bounded(plan_path, PLAN_LIMIT)
        plan = _parse_json(raw_plan)
        if not isinstance(plan, dict) or plan.get("schema_version") != 1:
            raise AuditInputError("invalid_plan_schema")
        if set(plan) != {"schema_version", "expected_catalog_sha256", "inputs"}:
            raise AuditInputError("unexpected_plan_fields")
        for name, path in roots.items():
            if name == "plan" or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", name):
                raise AuditInputError("invalid_root_name")
            if _contains_symlink(path) or not path.is_dir():
                raise AuditInputError("invalid_root_directory")
        loaded, issues = _load_inputs(plan, plan_path, roots)
        _check_evidence(plan, loaded, issues)
        status = "blocked" if issues else "ready_for_operator_review"
        return {
            "schema_version": 1,
            "status": status,
            "scope": "local_preparation_only",
            "roles": {
                role: {
                    "status": "bound" if role in loaded else "missing_or_unreadable",
                    "sha256": loaded[role]["sha256"] if role in loaded else None,
                }
                for role in ROLES
            },
            "issues": sorted(issues, key=lambda item: (item["role"], item["code"])),
            "operator_must_verify_records_and_consent_at_session_time": True,
            "participant_sessions_admitted": False,
            "public_release_approved": False,
            "authenticity_verified": False,
        }
    except AuditInputError as exc:
        return {
            "schema_version": 1,
            "status": "invalid",
            "scope": "local_preparation_only",
            "roles": {},
            "issues": [{"role": "plan", "code": str(exc)}],
            "participant_sessions_admitted": False,
            "public_release_approved": False,
            "authenticity_verified": False,
        }


def _parse_root(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("root must be NAME=ABSOLUTE_DIRECTORY")
    name, raw_path = value.split("=", 1)
    path = Path(raw_path)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("root directory must be absolute")
    return name, path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit local study-preparation evidence")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--root", action="append", default=[], type=_parse_root)
    args = parser.parse_args(argv)
    roots: dict[str, Path] = {}
    for name, path in args.root:
        if name in roots:
            parser.error("duplicate --root name")
        roots[name] = path
    report = audit_plan(args.plan, roots)
    report_path = args.report.expanduser().absolute()
    try:
        payload = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        fd = os.open(report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
    except FileExistsError:
        print("report_exists", file=sys.stderr)
        return 1
    except OSError:
        print("report_write_failed", file=sys.stderr)
        return 1
    print(json.dumps({"status": report["status"], "report": str(report_path)}, ensure_ascii=False))
    return {"ready_for_operator_review": 0, "blocked": 2, "invalid": 1}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
