"""Check a loopback study process; outputs are fictional functional evidence only."""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

CASES = {
    "repayment-A": ["income_decreased", "payment_due", "not_overdue", "first_consultation"],
    "repayment-B": ["income_decreased", "payment_due", "not_overdue", "records_unorganized"],
    "collection-A": ["overdue", "collection_contact", "caller_unclear", "records_unorganized"],
    "collection-B": ["overdue", "collection_contact", "caller_unclear", "repeated_contact"],
    "procedure-A": ["procedure_unclear", "working", "records_unorganized", "first_consultation"],
    "procedure-B": ["procedure_unclear", "multiple_payments", "records_unorganized", "working"],
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = build_opener(ProxyHandler({}), NoRedirect())


def call(base: str, path: str, payload=None, raw=None):
    body = json.dumps(payload).encode() if payload is not None else raw
    req = Request(base + path, data=body)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    started = time.monotonic()
    try:
        response = _OPENER.open(req, timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        if 300 <= response.status < 400:
            raise RuntimeError("loopback checker refuses redirects")
        content = response.read()
        headers = {key.lower(): value for key, value in response.headers.items()}
        result = json.loads(content)
        code = response.status
    assert headers.get("cache-control", "").startswith("no-store")
    assert headers.get("x-request-id")
    return code, result, round(time.monotonic() - started, 6), headers["x-request-id"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8011")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if (url.scheme != "http" or url.hostname != "127.0.0.1" or url.username
            or url.password or url.query or url.fragment or url.path not in ("", "/")):
        parser.error("only an explicit loopback HTTP study process is allowed")
    if args.output.exists():
        parser.error("refusing to replace earlier evidence; choose a new output file")
    base = args.base_url.rstrip("/")
    code, health, _, _ = call(base, "/health")
    assert code == 200 and health["mode"] == "preparation_only"
    assert health["external_model_enabled"] is False
    code, opts, _, _ = call(base, "/api/preparation/options")
    assert code == 200 and len(opts["topics"]) == 3
    known_facts = {fact["id"]: fact["label"] for fact in opts["facts"]}
    outputs = []
    for case_id, facts in CASES.items():
        issue = case_id.rsplit("-", 1)[0]
        code, card, elapsed, request_id = call(
            base, "/api/preparation/card", {"issue": issue, "selected_facts": facts}
        )
        assert code == 200 and card["status"] == "ready"
        assert card["catalog_sha256"] == opts["catalog_sha256"]
        assert [f["id"] for f in card["selected_facts"]] == facts
        assert all(f["label"] == known_facts[f["id"]] for f in card["selected_facts"])
        assert all(f["provenance"] == "user_selected_unverified" for f in card["selected_facts"])
        ids = {s["id"] for s in card["sources"]}
        assert card["official_route"]["source_id"] in ids
        for field in ("to_confirm", "preparation_items", "counselor_questions", "uncertainties"):
            assert all(i.get("kind") != "official_guidance" or i.get("source_id") in ids
                       for i in card[field])
        outputs.append({"case_id": case_id, "http_status": code, "seconds": elapsed,
                        "request_id": request_id, "card": card})

    code, partial, _, _ = call(base, "/api/preparation/card",
                               {"issue": "repayment", "selected_facts": []})
    assert code == 200 and partial["status"] == "needs_details" and not partial["selected_facts"]
    code, invalid, _, _ = call(base, "/api/preparation/card", {
        "issue": "repayment", "selected_facts": ["not_overdue", "overdue"],
    })
    assert code == 422 and invalid == {"error": "invalid_request"}
    code, invalid, _, _ = call(base, "/api/preparation/card", {
        "issue": "repayment", "selected_facts": [], "question": "FICTIONAL_MARKER",
    })
    assert code == 422 and invalid == {"error": "invalid_request"}
    code, large, _, _ = call(base, "/api/preparation/card", raw=b"x" * 9000)
    assert code == 413 and large == {"error": "invalid_request"}
    code, _, _, _ = call(base, "/api/consult", {"question": "fictional"})
    assert code == 404

    payload = {"issue": "repayment", "selected_facts": ["payment_due", "not_overdue"]}
    with ThreadPoolExecutor(max_workers=6) as pool:
        parallel = list(pool.map(lambda _: call(base, "/api/preparation/card", payload), range(6)))
    assert all(row[0] == 200 for row in parallel)
    assert len({row[1]["card_id"] for row in parallel}) == 1
    assert len({row[3] for row in parallel}) == 6

    report = {
        "scope": "functional_contract_only_not_legal_accuracy_or_user_outcomes",
        "fictional_inputs_only": True, "actual_user_sessions": 0,
        "selection_projection": (
            "manual selection from exposed fictional scenarios; no NLP extraction"
        ),
        "result": "pass", "health": health, "cases": outputs,
        "checks": {"six_case_contracts": True, "missing_facts_not_invented": True,
                   "contradictory_facts_rejected": True, "free_text_rejected": True,
                   "oversized_body_rejected": True, "legacy_model_route_unavailable": True,
                   "six_parallel_calls_same_card_distinct_request_ids": True},
        "professional_review": "pending", "scenario_gold_keys": "unapproved",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"result": "pass", "fictional_cases": len(outputs),
                      "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
