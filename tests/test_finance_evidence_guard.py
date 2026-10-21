from __future__ import annotations

import hashlib
import json

import pytest

from finance_evidence.contracts import digest, text_digest, verify_case
from finance_evidence.coverage import assess_coverage
from finance_evidence.runner import main, run_guarded


def _row(article: str, text: str, *, date: str = "20260301") -> dict:
    return {
        "law_name": "채무자 회생 및 파산에 관한 법률",
        "article": article,
        "enforced_date": date,
        "source_url": "https://example.invalid/law",
        "text": text,
    }


def _policy(corpus: list[dict], *, status: str = "approved",
            basis: str = "pre_generation", partial: bool = False) -> dict:
    def ref(row: dict, scope: str = "personal-rehabilitation") -> dict:
        return {key: row[key] for key in (
            "law_name", "article", "enforced_date", "source_url"
        )} | {"text_sha256": text_digest(row["text"]), "procedure_scope": scope}

    return {
        "schema_version": "finance-evidence-requirements.v1",
        "corpus_sha256": "0" * 64,
        "snapshot_sha256": "1" * 64,
        "case_id": "prospect-008",
        "question_sha256": text_digest("질문 원문"),
        "review_status": status,
        "review_basis": basis,
        "reviewer_reference": "review-record-1",
        "partial_units_allowed": partial,
        "units": [
            {"unit_id": "application", "procedure_scope": "personal-rehabilitation",
             "required_groups": [
                 {"group_id": "filing", "references": [ref(corpus[0]), ref(corpus[1])]},
                 {"group_id": "prior-history", "references": [ref(corpus[2])]},
             ]},
        ],
    }


def _verified(policy: dict, corpus: list[dict], chunks: list[dict]) -> dict:
    return assess_coverage(policy, corpus, chunks)


def _bound_inputs(policy: dict, corpus: list[dict], chunks: list[dict],
                  question: str = "질문 원문") -> tuple[dict, bytes, bytes]:
    corpus_raw = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in corpus).encode()
    snapshot = {
        "schema_version": "retrieval-snapshot.v1",
        "manifest_sha256": ["a" * 64],
        "conditions": {
            key: "b" * 64 for key in (
                "index_sha256", "embedding_weights_sha256", "embedding_configuration_sha256",
                "retrieval_configuration_sha256", "runtime_sha256",
            )
        },
        "sources": {
            key: "c" * 64 for key in (
                "corpus_sha256", "search_code_sha256", "finance_code_sha256", "synonyms_sha256",
                "base_agent_answer_sha256", "llm_code_sha256",
            )
        },
        "cases": [{"case_id": "prospect-008", "user_input": question,
                   "chunks": [dict(chunk, score=0.9) for chunk in chunks]}],
    }
    snapshot["sources"]["corpus_sha256"] = digest(corpus_raw)
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
    snapshot["payload_sha256"] = digest(payload)
    snapshot_raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode()
    bound_policy = json.loads(json.dumps(policy))
    bound_policy["corpus_sha256"] = digest(corpus_raw)
    bound_policy["snapshot_sha256"] = digest(snapshot_raw)
    bound_policy["question_sha256"] = text_digest(question)
    return bound_policy, snapshot_raw, corpus_raw


def test_alternative_references_are_or_and_groups_are_and() -> None:
    corpus = [_row("제589조", "신청서"), _row("제590조", "예납"), _row("제579조", "대체")]
    policy = _policy(corpus)
    result = _verified(policy, corpus, [corpus[1], corpus[2]])
    assert result["unit_results"][0]["state"] == "covered"
    assert [g["state"] for g in result["unit_results"][0]["groups"]] == [
        "covered", "covered"
    ]

    result = _verified(policy, corpus, [corpus[0]])
    assert result["unit_results"][0]["state"] == "missing"
    assert result["unit_results"][0]["missing_groups"] == ["prior-history"]


@pytest.mark.parametrize("change", ["body", "date", "source"])
def test_changed_reviewed_reference_is_unknown(change: str) -> None:
    corpus = [_row("제589조", "신청서"), _row("제590조", "예납"), _row("제579조", "정의")]
    policy = _policy(corpus)
    tampered = [dict(row) for row in corpus]
    if change == "body":
        tampered[0]["text"] += " 변조"
    elif change == "date":
        tampered[0]["enforced_date"] = "20270101"
    else:
        tampered[0]["source_url"] = "https://example.invalid/other"
    result = _verified(policy, tampered, [tampered[0]])
    assert result["unit_results"][0]["state"] == "unknown"
    assert result["model_authorized"] is False


def test_requirement_scope_mismatch_fails_before_generation() -> None:
    corpus = [_row("제589조", "신청서"), _row("제590조", "예납"), _row("제579조", "정의")]
    policy = _policy(corpus)
    policy["units"][0]["required_groups"][0]["references"][0]["procedure_scope"] = "other"
    policy, snapshot, corpus_raw = _bound_inputs(policy, corpus, corpus)
    calls = []
    with pytest.raises(ValueError, match="procedure scope mismatch"):
        run_guarded(policy, snapshot, corpus_raw, calls.append)
    assert calls == []


def test_empty_evidence_abstains_and_pending_or_retrospective_never_calls_model() -> None:
    corpus = [_row("제589조", "신청서"), _row("제590조", "예납"), _row("제579조", "정의")]
    for status, basis in (("pending", "pre_generation"),
                          ("approved", "retrospective_regression")):
        policy = _policy(corpus, status=status, basis=basis)
        policy, snapshot, corpus_raw = _bound_inputs(policy, corpus, [corpus[1], corpus[2]])
        calls = []
        result = run_guarded(policy, snapshot, corpus_raw, calls.append)
        assert result["answer"] is None
        assert calls == []
    assert _verified(_policy(corpus), corpus, [])["disposition"] == "abstained"


def test_guard_rejects_forged_coverage_and_wrong_question_without_callback() -> None:
    corpus = [_row("제589조", "신청서"), _row("제590조", "예납"), _row("제579조", "정의")]
    policy = _policy(corpus)
    policy, snapshot, corpus_raw = _bound_inputs(policy, corpus, [corpus[1], corpus[2]])
    calls = []
    forged = json.dumps({"model_authorized": True, "disposition": "complete_evidence"}).encode()
    with pytest.raises(ValueError):
        run_guarded(policy, forged, corpus_raw, calls.append)
    assert calls == []

    policy["question_sha256"] = text_digest("different question")
    with pytest.raises(ValueError, match="question fingerprint mismatch"):
        run_guarded(policy, snapshot, corpus_raw, calls.append)
    assert calls == []


def test_pending_policy_with_partial_permission_never_returns_extractive_evidence() -> None:
    corpus = [_row("제589조", "신청서"), _row("제590조", "예납"), _row("제579조", "정의")]
    policy = _policy(corpus, status="pending", partial=True)
    policy["units"] = [
        {"unit_id": "filing", "procedure_scope": "personal-rehabilitation",
         "required_groups": [{"group_id": "application", "references": [
             {"law_name": corpus[0]["law_name"], "article": corpus[0]["article"],
              "enforced_date": corpus[0]["enforced_date"], "source_url": corpus[0]["source_url"],
              "text_sha256": text_digest(corpus[0]["text"]),
              "procedure_scope": "personal-rehabilitation"}
         ]}]},
        {"unit_id": "fees", "procedure_scope": "personal-rehabilitation",
         "required_groups": [{"group_id": "fee-rule", "references": [
             {"law_name": corpus[1]["law_name"], "article": corpus[1]["article"],
              "enforced_date": corpus[1]["enforced_date"], "source_url": corpus[1]["source_url"],
              "text_sha256": text_digest(corpus[1]["text"]),
              "procedure_scope": "personal-rehabilitation"}
         ]}]},
    ]
    policy, snapshot, corpus_raw = _bound_inputs(policy, corpus, [corpus[0]])
    calls = []
    result = run_guarded(policy, snapshot, corpus_raw, calls.append)
    assert result == {"disposition": "review_pending", "answer": None}
    assert calls == []


def test_partial_mode_returns_only_verbatim_supported_units_without_model() -> None:
    corpus = [_row("제589조", "원문 그대로"), _row("제590조", "비용 예납"),
              _row("제579조", "정의")]
    policy = _policy(corpus, partial=True)
    policy["units"] = [
        {"unit_id": "filing", "procedure_scope": "personal-rehabilitation",
         "required_groups": [{"group_id": "application", "references": [
             {"law_name": corpus[0]["law_name"], "article": corpus[0]["article"],
              "enforced_date": corpus[0]["enforced_date"], "source_url": corpus[0]["source_url"],
              "text_sha256": text_digest(corpus[0]["text"]),
              "procedure_scope": "personal-rehabilitation"}
         ]}]},
        {"unit_id": "fees", "procedure_scope": "personal-rehabilitation",
         "required_groups": [{"group_id": "fee-rule", "references": [
            {"law_name": corpus[1]["law_name"], "article": corpus[1]["article"],
             "enforced_date": corpus[1]["enforced_date"], "source_url": corpus[1]["source_url"],
             "text_sha256": text_digest(corpus[1]["text"]),
             "procedure_scope": "personal-rehabilitation"}
        ]}],
        },
    ]
    calls = []
    policy, snapshot, corpus_raw = _bound_inputs(policy, corpus, [corpus[0]])
    result = run_guarded(policy, snapshot, corpus_raw, calls.append)
    assert result["disposition"] == "partial_evidence"
    assert result["supported_evidence"][0]["citations"][0]["text"] == "원문 그대로"
    assert result["withheld_units"] == ["fees"]
    assert calls == []


def test_generation_payload_has_no_gold_or_policy_fields() -> None:
    corpus = [_row("제589조", "신청서"), _row("제590조", "예납"), _row("제579조", "정의")]
    policy = _policy(corpus)
    received = []

    def generate(payload: dict) -> str:
        received.append(payload)
        return "generated"

    policy, snapshot, corpus_raw = _bound_inputs(policy, corpus, [corpus[1], corpus[2]])
    result = run_guarded(policy, snapshot, corpus_raw, generate)
    assert result == {"disposition": "complete_evidence", "answer": "generated"}
    assert set(received[0]) == {"question", "evidence", "instructions"}
    assert received[0]["question"] == "질문 원문"
    assert {item["article"] for item in received[0]["evidence"]} == {"제590조", "제579조"}
    assert "expected" not in json.dumps(received[0])
    assert "required_groups" not in json.dumps(received[0])
    assert "application" not in json.dumps(received[0])


def test_case_fingerprint_binding_rejects_changed_corpus_or_question() -> None:
    corpus = [_row("제589조", "신청서"), _row("제590조", "예납"), _row("제579조", "정의")]
    policy = _policy(corpus)
    case = {"case_id": "prospect-008", "user_input": "질문 원문", "chunks": []}
    with pytest.raises(ValueError, match="corpus fingerprint mismatch"):
        verify_case(policy, [case], "f" * 64, policy["snapshot_sha256"])
    with pytest.raises(ValueError, match="question fingerprint mismatch"):
        verify_case(policy, [{**case, "user_input": "변경된 질문"}],
                    policy["corpus_sha256"], policy["snapshot_sha256"])


def test_real_case_shape_marks_unretrieved_590_missing() -> None:
    corpus = [_row("제589조", "신청서"), _row("제590조", "예납"), _row("제579조", "정의")]
    policy = _policy(corpus)
    policy["units"] = [{
        "unit_id": "application-and-fees", "procedure_scope": "personal-rehabilitation",
        "required_groups": [
            {"group_id": "application", "references": [
                {"law_name": corpus[0]["law_name"], "article": corpus[0]["article"],
                 "enforced_date": corpus[0]["enforced_date"],
                 "source_url": corpus[0]["source_url"],
                 "text_sha256": text_digest(corpus[0]["text"]),
                 "procedure_scope": "personal-rehabilitation"}
            ]},
            {"group_id": "advance-costs", "references": [
                {"law_name": corpus[1]["law_name"], "article": corpus[1]["article"],
                 "enforced_date": corpus[1]["enforced_date"],
                 "source_url": corpus[1]["source_url"],
                 "text_sha256": text_digest(corpus[1]["text"]),
                 "procedure_scope": "personal-rehabilitation"}
            ]},
        ],
    }]
    result = _verified(policy, corpus, [corpus[0], _row("제39조", "관할"), corpus[2]])
    assert result["unit_results"][0]["state"] == "missing"
    assert result["unit_results"][0]["missing_groups"] == ["advance-costs"]
    assert result["model_authorized"] is False


def test_cli_checks_snapshot_and_corpus_fingerprints_and_writes_sanitized_report(
    tmp_path, capsys
) -> None:
    corpus = [_row("제589조", "신청서 원문"), _row("제590조", "비용 예납 원문"),
              _row("제39조", "관할 원문"), _row("제579조", "정의 원문")]
    corpus_path = tmp_path / "corpus.jsonl"
    corpus_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in corpus))
    corpus_hash = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
    snapshot = {
        "schema_version": "retrieval-snapshot.v1",
        "manifest_sha256": ["a" * 64],
        "conditions": {
            key: "b" * 64 for key in (
                "index_sha256", "embedding_weights_sha256", "embedding_configuration_sha256",
                "retrieval_configuration_sha256", "runtime_sha256",
            )
        },
        "sources": {
            key: "c" * 64 for key in (
                "corpus_sha256", "search_code_sha256", "finance_code_sha256", "synonyms_sha256",
                "base_agent_answer_sha256", "llm_code_sha256",
            )
        },
        "cases": [{
            "case_id": "prospect-008", "user_input": "질문 원문",
            "chunks": [dict(row, score=0.9) for row in (corpus[0], corpus[2], corpus[3])],
        }],
    }
    snapshot["sources"]["corpus_sha256"] = corpus_hash
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
    snapshot["payload_sha256"] = digest(payload)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
    snapshot_hash = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    policy = {
        "schema_version": "finance-evidence-requirements.v1",
        "corpus_sha256": corpus_hash,
        "snapshot_sha256": snapshot_hash,
        "case_id": "prospect-008",
        "question_sha256": text_digest("질문 원문"),
        "review_status": "pending",
        "review_basis": "retrospective_regression",
        "reviewer_reference": "private-review-record",
        "partial_units_allowed": False,
        "units": [{
            "unit_id": "application-and-fees", "procedure_scope": "personal-rehabilitation",
            "required_groups": [
                {"group_id": "application", "references": [
                    {**{key: corpus[0][key] for key in (
                        "law_name", "article", "enforced_date", "source_url"
                    )}, "text_sha256": text_digest(corpus[0]["text"]),
                     "procedure_scope": "personal-rehabilitation"}
                ]},
                {"group_id": "advance-costs", "references": [
                    {**{key: corpus[1][key] for key in (
                        "law_name", "article", "enforced_date", "source_url"
                    )}, "text_sha256": text_digest(corpus[1]["text"]),
                     "procedure_scope": "personal-rehabilitation"}
                ]},
            ],
        }],
    }
    policy_path = tmp_path / "requirements.json"
    policy_path.write_text(json.dumps(policy, ensure_ascii=False))
    report_path = tmp_path / "report.json"
    code = main(["--snapshot", str(snapshot_path), "--corpus", str(corpus_path),
                 "--requirements", str(policy_path), "--case-id", "prospect-008",
                 "--report", str(report_path)])
    report = json.loads(report_path.read_text())
    assert code == 2
    assert report["disposition"] == "review_pending"
    assert report["unit_results"][0]["missing_groups"] == ["advance-costs"]
    assert "제590조" not in {
        citation["article"] for citation in report["unit_results"][0]["matched_citations"]
    }
    assert "제39조" not in {
        citation["article"] for citation in report["unit_results"][0]["matched_citations"]
    }
    assert report["procedure_scope_authority"] == "operator_requirements_attestation_only"
    assert "no verified procedure scope" in report["procedure_scope_note"]
    assert report["semantic_legal_validation"] is False
    assert "질문 원문" not in report_path.read_text()
    assert "신청서 원문" not in report_path.read_text()
    assert "private-review-record" not in report_path.read_text()
    assert json.loads(capsys.readouterr().out)["model_authorized"] is False


def test_cli_io_error_does_not_print_paths_or_private_markers(tmp_path, capsys) -> None:
    corpus = [_row("제589조", "신청서"), _row("제590조", "예납"), _row("제579조", "정의")]
    corpus_path = tmp_path / "corpus.jsonl"
    corpus_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in corpus))
    policy_path = tmp_path / "requirements.json"
    policy_path.write_text(json.dumps(_policy(corpus), ensure_ascii=False))
    private_marker = "reviewer-secret-marker"
    missing_snapshot = tmp_path / f"{private_marker}.json"
    code = main(["--snapshot", str(missing_snapshot), "--corpus", str(corpus_path),
                 "--requirements", str(policy_path), "--case-id", "prospect-008",
                 "--report", str(tmp_path / "report.json")])
    captured = capsys.readouterr()
    assert code == 1
    assert "input_io_error" in captured.err
    assert private_marker not in captured.err
    assert str(tmp_path) not in captured.err
