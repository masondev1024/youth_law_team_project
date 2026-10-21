"""Exact reference membership checks; no semantic or legal-correctness inference."""

from __future__ import annotations

from typing import Any

from .contracts import text_digest, validate_policy

REFERENCE_KEYS = ("law_name", "article", "enforced_date", "source_url")


def _identity(record: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (*[record[key] for key in REFERENCE_KEYS], text_digest(record["text"]))


def assess_coverage(policy: dict[str, Any], corpus: list[dict[str, Any]],
                    retrieved_chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify required units by exact body/metadata membership in corpus and retrieval."""
    policy = validate_policy(policy)
    corpus_index = {_identity(row): row for row in corpus}
    retrieval_index = {_identity(row): row for row in retrieved_chunks}
    retrieval_metadata = {tuple(row[key] for key in REFERENCE_KEYS) for row in retrieved_chunks}
    unit_results = []
    for unit in policy["units"]:
        group_results = []
        matched: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        for group in unit["required_groups"]:
            candidate_matches = []
            candidate_is_unknown = False
            for ref in group["references"]:
                key = (ref["law_name"], ref["article"], ref["enforced_date"],
                       ref["source_url"], ref["text_sha256"])
                source = corpus_index.get(key)
                if source is None:
                    # A reviewed reference absent from this corpus (body, date, or
                    # source changed) is unverifiable, never an ordinary retrieval miss.
                    candidate_is_unknown = True
                    continue
                chunk = retrieval_index.get(key)
                if chunk is None:
                    if key[:4] in retrieval_metadata:
                        candidate_is_unknown = True
                    continue
                matched[key] = chunk
                candidate_matches.append(key)
            if candidate_matches:
                state = "covered"
            elif candidate_is_unknown:
                state = "unknown"
            else:
                state = "missing"
            group_results.append({"group_id": group["group_id"], "state": state})
        states = [group["state"] for group in group_results]
        if "unknown" in states:
            state = "unknown"
        elif "missing" in states:
            state = "missing"
        else:
            state = "covered"
        unit_results.append({
            "unit_id": unit["unit_id"],
            "state": state,
            "groups": group_results,
            "matched_citations": [
                {**{key: chunk[key] for key in REFERENCE_KEYS},
                 "text_sha256": key[-1], "text": chunk["text"]}
                for key, chunk in sorted(matched.items())
            ],
            "missing_groups": [g["group_id"] for g in group_results if g["state"] == "missing"],
        })
    statuses = [unit["state"] for unit in unit_results]
    all_covered = all(state == "covered" for state in statuses)
    authorized = (policy["review_status"] == "approved"
                  and policy["review_basis"] == "pre_generation")
    if not authorized:
        disposition = "review_pending"
    elif all_covered:
        disposition = "complete_evidence"
    elif "unknown" in statuses or not any(state == "covered" for state in statuses):
        disposition = "abstained"
    else:
        disposition = "partial_evidence"
    return {
        "disposition": disposition,
        "unit_results": unit_results,
        "model_authorized": authorized and all_covered,
        "partial_units_allowed": policy["partial_units_allowed"],
    }
