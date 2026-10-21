"""Guarded experiment runner and CLI for finance evidence coverage."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from .contracts import (
    MAX_CORPUS_BYTES,
    MAX_POLICY_BYTES,
    MAX_SNAPSHOT_BYTES,
    digest,
    load_corpus,
    load_json,
    parse_json,
    read_bounded,
    validate_policy,
    validate_snapshot,
    verify_case,
)
from .coverage import assess_coverage

GENERATION_INSTRUCTIONS = (
    "Answer only from the supplied evidence. If it does not support an answer, say so. "
    "Provide legal information, not individualized legal advice."
)


def evaluate_inputs(policy_value: Any, snapshot_raw: bytes,
                    corpus_raw: bytes) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Validate all bound inputs, then recompute exact coverage from their bytes."""
    if len(snapshot_raw) > MAX_SNAPSHOT_BYTES or len(corpus_raw) > MAX_CORPUS_BYTES:
        raise ValueError("input byte limit exceeded")
    policy = validate_policy(policy_value)
    snapshot_value = parse_json(snapshot_raw, "snapshot")
    cases = validate_snapshot(snapshot_value)
    corpus = load_corpus(corpus_raw)
    snapshot_hash, corpus_hash = digest(snapshot_raw), digest(corpus_raw)
    if snapshot_value["sources"]["corpus_sha256"] != corpus_hash:
        raise ValueError("snapshot corpus fingerprint mismatch")
    case = verify_case(policy, cases, corpus_hash, snapshot_hash)
    coverage = assess_coverage(policy, corpus, case["chunks"])
    return policy, case, coverage


def run_guarded(policy_value: Any, snapshot_raw: bytes, corpus_raw: bytes,
                generate: Callable[[dict[str, Any]], str]) -> dict[str, Any]:
    """Recompute all evidence inputs before calling generation or returning partial text."""
    policy, case, coverage = evaluate_inputs(policy_value, snapshot_raw, corpus_raw)
    if coverage["model_authorized"] and coverage["disposition"] == "complete_evidence":
        supported = []
        for unit in coverage["unit_results"]:
            for citation in unit["matched_citations"]:
                supported.append({key: citation[key] for key in (
                    "law_name", "article", "enforced_date", "source_url", "text"
                )})
        payload = {
            "question": case["user_input"],
            "evidence": supported,
            "instructions": GENERATION_INSTRUCTIONS,
        }
        return {"disposition": "complete_evidence", "answer": generate(payload)}

    partial_allowed = (
        coverage["disposition"] == "partial_evidence"
        and policy["review_status"] == "approved"
        and policy["review_basis"] == "pre_generation"
        and policy["partial_units_allowed"]
    )
    if partial_allowed:
        available = [
            {"unit_id": unit["unit_id"], "citations": [
                {key: citation[key] for key in (
                    "law_name", "article", "enforced_date", "source_url", "text"
                )} for citation in unit["matched_citations"]
            ]}
            for unit in coverage["unit_results"] if unit["state"] == "covered"
        ]
        withheld = [unit["unit_id"] for unit in coverage["unit_results"]
                    if unit["state"] != "covered"]
        if available:
            return {"disposition": "partial_evidence", "supported_evidence": available,
                    "withheld_units": withheld}
    return {"disposition": coverage["disposition"], "answer": None}


def _report(policy: dict[str, Any], coverage: dict[str, Any], case: dict[str, Any],
            corpus_hash: str, snapshot_hash: str) -> dict[str, Any]:
    units = []
    for unit in coverage["unit_results"]:
        units.append({
            "unit_id": unit["unit_id"], "state": unit["state"],
            "groups": unit["groups"], "missing_groups": unit["missing_groups"],
            "matched_citations": [{key: citation[key] for key in (
                "law_name", "article", "enforced_date", "source_url", "text_sha256"
            )} for citation in unit["matched_citations"]],
        })
    return {
        "schema_version": "finance-evidence-report.v1",
        "case_id": policy["case_id"],
        "question_sha256": policy["question_sha256"],
        "corpus_sha256": corpus_hash,
        "snapshot_sha256": snapshot_hash,
        "review_status": policy["review_status"],
        "review_basis": policy["review_basis"],
        "procedure_scope_authority": "operator_requirements_attestation_only",
        "procedure_scope_note": (
            "Snapshot and corpus contain no verified procedure scope; scope labels are operator "
            "attestations and legal applicability is not evaluated."
        ),
        "semantic_legal_validation": False,
        "disposition": coverage["disposition"],
        "model_authorized": coverage["model_authorized"],
        "partial_units_allowed": policy["partial_units_allowed"],
        "retrieved_chunk_count": len(case["chunks"]),
        "unit_results": units,
    }


def run_files(snapshot_path: Path, corpus_path: Path, requirements_path: Path,
              report_path: Path) -> tuple[dict[str, Any], int]:
    input_paths = [snapshot_path.resolve(), corpus_path.resolve(), requirements_path.resolve()]
    if len(set(input_paths)) != len(input_paths):
        raise ValueError("input paths must be distinct")
    output = report_path.resolve()
    if output in set(input_paths):
        raise ValueError("report path collides with input")
    if output.exists():
        raise FileExistsError("report output already exists")
    snapshot_raw = read_bounded(snapshot_path, MAX_SNAPSHOT_BYTES, "snapshot")
    corpus_raw = read_bounded(corpus_path, MAX_CORPUS_BYTES, "corpus")
    policy_raw, policy_value = load_json(requirements_path, MAX_POLICY_BYTES, "requirements")
    del policy_raw
    policy, case, coverage = evaluate_inputs(policy_value, snapshot_raw, corpus_raw)
    snapshot_hash, corpus_hash = digest(snapshot_raw), digest(corpus_raw)
    report = _report(policy, coverage, case, corpus_hash, snapshot_hash)
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    with output.open("xb") as stream:
        stream.write(encoded)
    code = 0 if report["disposition"] == "complete_evidence" else 2
    return report, code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--requirements", required=True, type=Path)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        policy_raw, policy_value = load_json(args.requirements, MAX_POLICY_BYTES, "requirements")
        del policy_raw
        policy = validate_policy(policy_value)
        if args.case_id != policy["case_id"]:
            raise ValueError("case id does not match requirements")
        report, code = run_files(args.snapshot, args.corpus, args.requirements, args.report)
        print(json.dumps({"report_written": True, "exit_code": code,
                          "disposition": report["disposition"],
                          "model_authorized": report["model_authorized"]}, ensure_ascii=False))
        return code
    except OSError as exc:
        print(json.dumps({"error_code": "input_io_error",
                          "error_type": type(exc).__name__}), file=sys.stderr)
        return 1
    except ValueError as exc:
        print(json.dumps({"error_code": "invalid_input",
                          "error_type": type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
