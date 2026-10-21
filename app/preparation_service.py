"""Catalog-backed, deterministic preparation-card assembly.

This module intentionally has no model, graph, database, session, or logging
dependencies. User-selected facts remain unverified and are never inferred.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.preparation_models import PreparationCardRequest, PreparationError

_CATALOG_PATH = (
    Path(__file__).resolve().parent
    / "resources"
    / "finance-guides-v1.json"
)
_MAX_CATALOG_AGE_DAYS = 30
_ISSUES = {"repayment", "collection", "procedure"}
_OFFICIAL_HOSTS = {
    "fsc.go.kr",
    "fss.or.kr",
    "kinfa.or.kr",
    "ccrs.or.kr",
    "klac.or.kr",
    "scourt.go.kr",
    "law.go.kr",
}
_ISSUE_ITEM_MINIMUMS = {
    "to_confirm": 2,
    "preparation_items": 3,
    "counselor_questions": 2,
    "uncertainties": 1,
}
_SEOUL = ZoneInfo("Asia/Seoul")


def _today() -> date:
    """Use the product's declared Korean local date for source freshness."""
    return datetime.now(_SEOUL).date()


def _unavailable() -> PreparationError:
    return PreparationError(503, "source_unavailable")


def _catalog_fingerprint(catalog: dict[str, Any]) -> str:
    canonical = json.dumps(
        catalog,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _is_official_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
        return (
            parsed.scheme == "https"
            and bool(host)
            and port in (None, 443)
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and any(host == base or host.endswith(f".{base}") for base in _OFFICIAL_HOSTS)
        )
    except ValueError:
        return False


def _load_catalog(
    path: Path | None = None,
    *,
    raw_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Read and validate the complete approved-source snapshot for each call."""
    try:
        snapshot = raw_bytes if raw_bytes is not None else (path or _CATALOG_PATH).read_bytes()
        catalog = json.loads(snapshot)
        if not isinstance(catalog, dict):
            raise ValueError("catalog must be an object")
        if catalog.get("review_status") != "official_pages_checked":
            raise ValueError("source review incomplete")
        version = catalog.get("version")
        checked_on_raw = catalog["checked_on"]
        if not isinstance(checked_on_raw, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", checked_on_raw
        ):
            raise ValueError("invalid checked_on")
        checked_on = date.fromisoformat(checked_on_raw)
        age = (_today() - checked_on).days
        if (
            not isinstance(version, str)
            or not version.strip()
            or age < 0
            or age > _MAX_CATALOG_AGE_DAYS
        ):
            raise ValueError("catalog version or freshness invalid")

        sources = catalog.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError("no sources")
        source_by_id: dict[str, dict[str, Any]] = {}
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError("invalid source")
            source_id = source.get("id")
            if (
                not isinstance(source_id, str)
                or not source_id
                or len(source_id) > 128
                or source_id in source_by_id
                or source.get("verified") is not True
                or source.get("review_kind") != "official_page_check"
                or not _is_official_url(source.get("url"))
                or source.get("checked_on") != catalog["checked_on"]
                or not isinstance(source.get("title"), str)
                or not source["title"].strip()
            ):
                raise ValueError("invalid or unverified source")
            source_by_id[source_id] = source

        topics = catalog.get("topics")
        facts = catalog.get("facts")
        issue_catalog = catalog.get("issues")
        if (
            not isinstance(topics, list)
            or not topics
            or not isinstance(facts, list)
            or not facts
        ):
            raise ValueError("invalid topics or facts")
        if not isinstance(issue_catalog, dict) or set(issue_catalog) != _ISSUES:
            raise ValueError("invalid issue catalog")
        topic_ids = set()
        for topic in topics:
            if not isinstance(topic, dict) or not all(
                isinstance(topic.get(key), str) and topic[key].strip()
                for key in ("id", "label", "description")
            ):
                raise ValueError("invalid topic")
            if len(topic["id"]) > 128:
                raise ValueError("topic id too long")
            if topic["id"] in topic_ids:
                raise ValueError("duplicate topic")
            topic_ids.add(topic["id"])
        if topic_ids != _ISSUES:
            raise ValueError("topic set does not match issues")

        fact_ids = set()
        for fact in facts:
            if (
                not isinstance(fact, dict)
                or not isinstance(fact.get("id"), str)
                or not fact["id"]
                or len(fact["id"]) > 128
            ):
                raise ValueError("invalid fact")
            topic_refs = fact.get("topic_ids")
            if (
                fact["id"] in fact_ids
                or not isinstance(fact.get("label"), str)
                or not fact["label"].strip()
                or not isinstance(topic_refs, list)
                or not topic_refs
                or any(topic_id not in topic_ids for topic_id in topic_refs)
                or len(set(topic_refs)) != len(topic_refs)
            ):
                raise ValueError("invalid fact")
            fact_ids.add(fact["id"])

        _validate_exclusive_groups(catalog.get("mutually_exclusive_fact_groups"), fact_ids)

        for issue, details in issue_catalog.items():
            _validate_issue(issue, details, source_by_id)
        return catalog
    except Exception:
        raise _unavailable() from None


def _validate_exclusive_groups(groups: Any, fact_ids: set[str]) -> None:
    if not isinstance(groups, list):
        raise ValueError("invalid mutually-exclusive fact groups")
    seen_groups: set[frozenset[str]] = set()
    for group in groups:
        if (
            not isinstance(group, list)
            or len(group) < 2
            or any(not isinstance(fact_id, str) or fact_id not in fact_ids for fact_id in group)
            or len(set(group)) != len(group)
            or frozenset(group) in seen_groups
        ):
            raise ValueError("invalid mutually-exclusive fact group")
        seen_groups.add(frozenset(group))


def _source_ref(source_id: Any, source_by_id: dict[str, dict[str, Any]]) -> bool:
    return isinstance(source_id, str) and source_id in source_by_id


def _validate_items(
    items: Any,
    source_by_id: dict[str, dict[str, Any]],
    *,
    allow_official: bool = True,
) -> None:
    if not isinstance(items, list) or len(items) < 1:
        raise ValueError("invalid items")
    seen: set[str] = set()
    seen_text: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid item")
        item_id, text, kind = item.get("id"), item.get("text"), item.get("kind")
        if (
            not isinstance(item_id, str)
            or not item_id
            or len(item_id) > 128
            or item_id in seen
            or not isinstance(text, str)
            or not text.strip()
            or len(text) > 2000
            or kind not in {"official_guidance", "service_suggestion"}
        ):
            raise ValueError("invalid item")
        normalized_text = " ".join(text.split()).casefold()
        if normalized_text in seen_text:
            raise ValueError("duplicate item text")
        seen.add(item_id)
        seen_text.add(normalized_text)
        if kind == "official_guidance" and (
            not allow_official or not _source_ref(item.get("source_id"), source_by_id)
        ):
            raise ValueError("unreferenced official claim")
        if "source_id" in item and not _source_ref(item.get("source_id"), source_by_id):
            raise ValueError("unknown item source")


def _validate_issue(
    issue: str,
    details: Any,
    source_by_id: dict[str, dict[str, Any]],
) -> None:
    if not isinstance(details, dict):
        raise ValueError("invalid issue")
    route = details.get("official_route")
    if (
        not isinstance(route, dict)
        or not all(isinstance(route.get(key), str) and route[key].strip()
                   for key in ("organization", "purpose"))
        or not _is_official_url(route.get("url"))
        or not _source_ref(route.get("source_id"), source_by_id)
        or source_by_id[route["source_id"]]["url"] != route["url"]
    ):
        raise ValueError("invalid route")
    for field, minimum in _ISSUE_ITEM_MINIMUMS.items():
        items = details.get(field)
        if not isinstance(items, list) or len(items) < minimum:
            raise ValueError("insufficient issue items")
        _validate_items(items, source_by_id)
    source_ids = details.get("source_ids")
    if (
        not isinstance(source_ids, list)
        or not source_ids
        or len(source_ids) != len(set(source_ids))
        or any(not _source_ref(source_id, source_by_id) for source_id in source_ids)
        or route["source_id"] not in source_ids
    ):
        raise ValueError("invalid issue sources")
    for field in _ISSUE_ITEM_MINIMUMS:
        for item in details[field]:
            if item.get("kind") == "official_guidance" and item["source_id"] not in source_ids:
                raise ValueError("source not declared for issue")


def options() -> dict[str, Any]:
    catalog = _load_catalog()
    return {
        "version": catalog["version"],
        "catalog_sha256": _catalog_fingerprint(catalog),
        "checked_on": catalog["checked_on"],
        "topics": [
            {key: topic[key] for key in ("id", "label", "description")}
            for topic in catalog["topics"]
        ],
        "facts": [
            {key: fact[key] for key in ("id", "label", "topic_ids")}
            for fact in catalog["facts"]
        ],
        "mutually_exclusive_fact_groups": [
            list(group) for group in catalog["mutually_exclusive_fact_groups"]
        ],
    }


def catalog_readiness() -> dict[str, str]:
    """Return safe health metadata only after the entire source snapshot validates."""
    catalog = _load_catalog()
    return {
        "status": "ready",
        "version": catalog["version"],
        "catalog_sha256": _catalog_fingerprint(catalog),
        "checked_on": catalog["checked_on"],
    }


def _parse_request(payload: Any) -> PreparationCardRequest:
    try:
        return PreparationCardRequest.model_validate(payload)
    except ValidationError:
        raise PreparationError(422, "invalid_request") from None


def build_card(payload: Any) -> dict[str, Any]:
    """Validate identifiers and assemble a stable card from reviewed catalog data."""
    request = _parse_request(payload)
    catalog = _load_catalog()
    facts_by_id = {fact["id"]: fact for fact in catalog["facts"]}
    selected_ids = request.selected_facts
    if (
        len(selected_ids) != len(set(selected_ids))
        or any(fact_id not in facts_by_id for fact_id in selected_ids)
        or any(request.issue not in facts_by_id[fact_id]["topic_ids"] for fact_id in selected_ids)
        or len(selected_ids) > 12
    ):
        raise PreparationError(422, "invalid_request")

    for group in catalog["mutually_exclusive_fact_groups"]:
        if len(set(selected_ids).intersection(group)) > 1:
            raise PreparationError(422, "invalid_request")

    details = catalog["issues"][request.issue]
    selected_facts = [
        {
            "id": fact_id,
            "label": facts_by_id[fact_id]["label"],
            "provenance": "user_selected_unverified",
        }
        for fact_id in selected_ids
    ]
    status = "ready" if len(selected_facts) >= 2 else "needs_details"
    catalog_sha256 = _catalog_fingerprint(catalog)
    card_seed = json.dumps(
        {
            "version": catalog["version"],
            "catalog_sha256": catalog_sha256,
            "issue": request.issue,
            "facts": selected_ids,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    card_id = "pc_" + hashlib.sha256(card_seed).hexdigest()[:20]
    source_by_id = {source["id"]: source for source in catalog["sources"]}
    return {
        "schema_version": "1",
        "version": catalog["version"],
        "catalog_sha256": catalog_sha256,
        "card_id": card_id,
        "status": status,
        "issue": request.issue,
        "selected_facts": selected_facts,
        "official_route": {
            key: details["official_route"][key]
            for key in ("organization", "url", "purpose", "source_id")
        },
        "to_confirm": details["to_confirm"],
        "preparation_items": details["preparation_items"],
        "counselor_questions": details["counselor_questions"],
        "uncertainties": details["uncertainties"],
        "sources": [
            {
                key: source_by_id[source_id][key]
                for key in ("id", "title", "url", "checked_on", "review_kind")
            }
            for source_id in details["source_ids"]
        ],
        "data_policy": (
            "선택한 항목은 사용자가 고른 미확인 정보입니다. 이 카드는 상담 준비 초안이며 "
            "법률 판단이나 신청 자격 승인 결과가 아닙니다. "
            "실제 개인정보나 금융정보를 입력하지 마세요."
        ),
    }
