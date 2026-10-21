"""Contract tests for the catalog-only preparation-card boundary."""

from __future__ import annotations

import ast
import asyncio
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.preparation_routes as preparation_routes
from app import preparation_service
from app.preparation_routes import router


@pytest.fixture
def catalog_file(tmp_path, monkeypatch):
    monkeypatch.setattr(preparation_service, "_today", lambda: date(2026, 10, 20))
    source = {
        "id": "official-test-source",
        "title": "공식 페이지 검토 테스트 자료",
        "url": "https://www.fsc.go.kr/test",
        "checked_on": "2026-10-20",
        "review_kind": "official_page_check",
        "verified": True,
    }
    topics = [
        {"id": issue, "label": f"{issue} topic", "description": "설명"}
        for issue in ("repayment", "collection", "procedure")
    ]
    facts = [
        {
            "id": "working",
            "label": "일하고 있습니다",
            "topic_ids": ["repayment", "collection", "procedure"],
        },
        {"id": "not_overdue", "label": "아직 연체되지 않았습니다", "topic_ids": ["repayment"]},
        {
            "id": "overdue",
            "label": "납부가 늦어졌습니다",
            "topic_ids": ["repayment", "collection", "procedure"],
        },
        {
            "id": "collection_contact",
            "label": "상환 요구 연락을 받았습니다",
            "topic_ids": ["collection"],
        },
    ]
    issues = {}
    for issue in ("repayment", "collection", "procedure"):
        issues[issue] = {
            "official_route": {
                "organization": "공식 상담 창구",
                "url": source["url"],
                "purpose": "상담 경로를 확인합니다",
                "source_id": source["id"],
            },
            "to_confirm": [
                {
                    "id": f"confirm-{issue}-{index}",
                    "text": f"상담에서 확인할 사항 {index}",
                    "kind": "service_suggestion",
                }
                for index in range(2)
            ],
            "preparation_items": [
                {
                    "id": f"prepare-{issue}-{index}",
                    "text": f"상담에 도움이 될 자료 후보 {index}",
                    "kind": "service_suggestion",
                }
                for index in range(3)
            ],
            "counselor_questions": [
                {
                    "id": f"question-{issue}-{index}",
                    "text": f"상담 담당자에게 물어볼 질문 {index}",
                    "kind": "service_suggestion",
                }
                for index in range(2)
            ],
            "uncertainties": [
                {
                    "id": f"uncertain-{issue}",
                    "text": "개별 조건은 상담 중 확인이 필요합니다",
                    "kind": "service_suggestion",
                }
            ],
            "source_ids": [source["id"]],
        }
    catalog = {
        "version": "finance-guides-test-v1",
        "checked_on": "2026-10-20",
        "review_status": "official_pages_checked",
        "topics": topics,
        "facts": facts,
        "issues": issues,
        "sources": [source],
        "mutually_exclusive_fact_groups": [["not_overdue", "overdue"]],
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(preparation_service, "_CATALOG_PATH", path)
    return path, catalog


@pytest.mark.parametrize(
    ("issue", "selected_facts"),
    [
        ("repayment", ["working", "not_overdue"]),
        ("collection", ["working", "overdue"]),
        ("procedure", ["working", "overdue"]),
    ],
)
def test_card_is_deterministic_for_each_issue_family(catalog_file, issue, selected_facts):
    _, catalog = catalog_file
    payload = {"issue": issue, "selected_facts": selected_facts}

    first = preparation_service.build_card(payload)
    second = preparation_service.build_card(payload)

    assert first == second
    assert first["status"] == "ready"
    assert len(first["catalog_sha256"]) == 64
    assert first["issue"] == issue
    assert [fact["id"] for fact in first["selected_facts"]] == selected_facts
    assert {fact["provenance"] for fact in first["selected_facts"]} == {"user_selected_unverified"}
    assert first["official_route"]["source_id"] in {s["id"] for s in first["sources"]}
    for field in ("to_confirm", "preparation_items", "counselor_questions", "uncertainties"):
        for item in first[field]:
            if item["kind"] == "official_guidance":
                assert item["source_id"] in catalog["issues"][issue]["source_ids"]


@pytest.mark.parametrize("selected_facts", [[], ["working"]])
def test_fewer_than_two_facts_needs_details(catalog_file, selected_facts):
    result = preparation_service.build_card(
        {"issue": "repayment", "selected_facts": selected_facts}
    )

    assert result["status"] == "needs_details"
    assert result["to_confirm"]
    assert "legal" not in result["data_policy"].lower()


@pytest.mark.parametrize(
    "payload",
    [
        {"issue": "unknown", "selected_facts": []},
        {"issue": "repayment", "selected_facts": ["not-in-catalog"]},
        {"issue": "repayment", "selected_facts": ["working", "working"]},
        {"issue": "repayment", "selected_facts": ["working", "collection_contact"]},
        {"issue": "repayment", "selected_facts": ["overdue", "not_overdue"]},
        {"issue": "repayment", "selected_facts": ["working"], "question": "개인 정보"},
        {"issue": "repayment", "selected_facts": ["working"], "session_id": "secret-session"},
        {"issue": "repayment", "selected_facts": ["working"], "amount": 12345},
        {"issue": "repayment", "selected_facts": ["working"] * 13},
    ],
)
def test_invalid_payloads_are_rejected(catalog_file, payload):
    with pytest.raises(preparation_service.PreparationError) as exc_info:
        preparation_service.build_card(payload)

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "invalid_request"


@pytest.mark.parametrize(
    ("mutation", "expected_status"),
    [
        (lambda doc: doc.update(checked_on="2026-08-01"), 503),
        (lambda doc: doc.update(review_status="draft"), 503),
        (lambda doc: doc["sources"][0].update(verified=False), 503),
        (lambda doc: doc["sources"][0].update(url="https://example.com/fake"), 503),
        (lambda doc: doc["issues"]["repayment"]["official_route"].update(source_id="missing"), 503),
    ],
)
def test_bad_or_stale_catalog_fails_closed(catalog_file, mutation, expected_status):
    path, catalog = catalog_file
    mutation(catalog)
    path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(preparation_service.PreparationError) as exc_info:
        preparation_service.options()

    assert exc_info.value.status_code == expected_status
    assert exc_info.value.code == "source_unavailable"


def test_missing_catalog_fails_closed(catalog_file):
    path, _ = catalog_file
    path.unlink()

    with pytest.raises(preparation_service.PreparationError) as exc_info:
        preparation_service.catalog_readiness()

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "source_unavailable"


def test_route_url_must_match_its_reviewed_source(catalog_file):
    path, catalog = catalog_file
    catalog["issues"]["repayment"]["official_route"]["url"] = "https://www.fsc.go.kr/other"
    path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(preparation_service.PreparationError) as exc_info:
        preparation_service.options()

    assert exc_info.value.status_code == 503


@pytest.mark.parametrize(
    "url",
    [
        "https://kinfa.or.kr.evil.or.kr/path",
        "https://evil.example/kinfa.or.kr",
        "https://www.fsc.go.kr:444/path",
        "https://user@www.fsc.go.kr/path",
        "http://www.fsc.go.kr/path",
    ],
)
def test_official_url_requires_allowlisted_host_and_safe_authority(url):
    assert preparation_service._is_official_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://kinfa.or.kr/path",
        "https://www.kinfa.or.kr/path",
        "https://www.fsc.go.kr:443/path",
    ],
)
def test_official_url_accepts_allowlisted_host_and_subdomains(url):
    assert preparation_service._is_official_url(url) is True


def test_catalog_fingerprint_tracks_copy_changes_even_when_version_is_unchanged(catalog_file):
    path, catalog = catalog_file
    payload = {"issue": "repayment", "selected_facts": ["working"]}
    before = preparation_service.build_card(payload)
    catalog["issues"]["repayment"]["preparation_items"][0]["text"] = "문구가 바뀌었습니다"
    path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    after = preparation_service.build_card(payload)

    assert before["version"] == after["version"]
    assert before["catalog_sha256"] != after["catalog_sha256"]
    assert before["card_id"] != after["card_id"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda doc: doc.pop("mutually_exclusive_fact_groups"),
        lambda doc: doc.update(mutually_exclusive_fact_groups=[{}]),
        lambda doc: doc["issues"]["repayment"].update(preparation_items=[]),
        lambda doc: doc["issues"]["repayment"]["counselor_questions"].__setitem__(
            1, dict(doc["issues"]["repayment"]["counselor_questions"][0])
        ),
    ],
)
def test_invalid_catalog_contract_is_unavailable(catalog_file, mutation):
    path, catalog = catalog_file
    mutation(catalog)
    path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(preparation_service.PreparationError) as exc_info:
        preparation_service.catalog_readiness()

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "source_unavailable"


def test_freshness_uses_seoul_local_date(catalog_file, monkeypatch):
    path, catalog = catalog_file
    catalog["checked_on"] = "2026-10-21"
    for source in catalog["sources"]:
        source["checked_on"] = "2026-10-21"
    path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(preparation_service, "_today", lambda: date(2026, 10, 20))

    with pytest.raises(preparation_service.PreparationError) as exc_info:
        preparation_service.options()

    assert exc_info.value.code == "source_unavailable"


def test_body_stream_limit_and_timeout(monkeypatch):
    async def too_large_stream():
        yield b"x" * 8193

    with pytest.raises(preparation_service.PreparationError) as size_error:
        asyncio.run(
            preparation_routes._read_request_body(SimpleNamespace(stream=too_large_stream))
        )
    assert size_error.value.status_code == 413

    async def slow_stream():
        await asyncio.sleep(0.05)
        yield b"{}"

    monkeypatch.setattr(preparation_routes, "_BODY_READ_TIMEOUT_SECONDS", 0.001)
    with pytest.raises(TimeoutError):
        asyncio.run(preparation_routes._read_request_body(SimpleNamespace(stream=slow_stream)))


def test_unverified_official_claim_without_source_fails_closed(catalog_file):
    path, catalog = catalog_file
    catalog["issues"]["repayment"]["preparation_items"][0] = {
        "id": "unsourced-claim",
        "text": "근거를 확인할 수 없는 절차상 주장",
        "kind": "official_guidance",
    }
    path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(preparation_service.PreparationError) as exc_info:
        preparation_service.options()

    assert exc_info.value.status_code == 503


def test_http_boundary_hides_invalid_input_and_never_calls_llm_or_database(catalog_file):
    app = FastAPI()
    app.include_router(router)

    client = TestClient(app)

    response = client.post(
        "/api/preparation/card",
        json={"issue": "repayment", "selected_facts": [], "question": "민감한 원문"},
    )

    assert response.status_code == 422
    assert response.json() == {"error": "invalid_request"}
    assert "민감한 원문" not in response.text
    assert response.headers["cache-control"] == "no-store, max-age=0"

    oversized = client.post("/api/preparation/card", content=b"x" * 8193)
    assert oversized.status_code == 413
    assert oversized.json() == {"error": "invalid_request"}

    source = Path(preparation_service.__file__).read_text(encoding="utf-8")
    imports = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.ImportFrom)]
    imported_modules = {node.module or "" for node in imports}
    assert not any(
        module == "graph"
        or module.startswith("agents.")
        or module in {"common.llm", "common.logging_store", "app.session_store"}
        for module in imported_modules
    )


def test_options_contract_and_cache_policy(catalog_file):
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/api/preparation/options")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "version",
        "catalog_sha256",
        "checked_on",
        "topics",
        "facts",
        "mutually_exclusive_fact_groups",
    }
    assert set(body["facts"][0]) == {"id", "label", "topic_ids"}
    assert body["mutually_exclusive_fact_groups"] == [["not_overdue", "overdue"]]
    assert response.headers["cache-control"] == "no-store, max-age=0"


def test_body_read_timeout_returns_generic_408(catalog_file, monkeypatch):
    async def timeout(_request):
        raise TimeoutError

    monkeypatch.setattr(preparation_routes, "_read_request_body", timeout)
    app = FastAPI()
    app.include_router(router)

    response = TestClient(app).post("/api/preparation/card", json={})

    assert response.status_code == 408
    assert response.json() == {"error": "request_timeout"}
