"""Strict, bounded contracts for an isolated finance evidence experiment."""

from __future__ import annotations

import hashlib
import json
import math
import numbers
import re
from pathlib import Path
from typing import Any

POLICY_SCHEMA = "finance-evidence-requirements.v1"
SNAPSHOT_SCHEMA = "retrieval-snapshot.v1"
SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
MAX_POLICY_BYTES = 1_000_000
MAX_SNAPSHOT_BYTES = 32_000_000
MAX_CORPUS_BYTES = 64_000_000
MAX_RECORDS = 100_000
MAX_CASES = 2_000
MAX_UNITS = 100
MAX_GROUPS = 100
MAX_REFERENCES = 100
MAX_POLICY_REFERENCES = 5_000
SNAPSHOT_CONDITIONS = {
    "index_sha256", "embedding_weights_sha256", "embedding_configuration_sha256",
    "retrieval_configuration_sha256", "runtime_sha256",
}
SNAPSHOT_SOURCES = {
    "corpus_sha256", "search_code_sha256", "finance_code_sha256", "synonyms_sha256",
    "base_agent_answer_sha256", "llm_code_sha256",
}
CASE_ID = re.compile(r"[A-Za-z0-9_-]+\Z", re.ASCII)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def text_digest(value: str) -> str:
    return digest(value.encode("utf-8"))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def parse_json(raw: bytes, label: str) -> Any:
    try:
        return json.loads(raw, object_pairs_hook=_unique_object,
                          parse_constant=lambda _value: (_ for _ in ()).throw(
                              ValueError("non-finite JSON number")))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"invalid {label} JSON") from exc


def load_json(path: Path, limit: int, label: str) -> tuple[bytes, Any]:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"{label} exceeds byte limit")
    return raw, parse_json(raw, label)


def read_bounded(path: Path, limit: int, label: str) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"{label} exceeds byte limit")
    return raw


def _object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"invalid {label} keys")
    return value


def _nonempty(value: Any, label: str, maximum: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"invalid {label}")
    return value


def _hash(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def validate_policy(value: Any) -> dict[str, Any]:
    keys = {
        "schema_version", "corpus_sha256", "snapshot_sha256", "case_id",
        "question_sha256", "review_status", "review_basis", "reviewer_reference",
        "partial_units_allowed", "units",
    }
    policy = _object(value, keys, "requirements")
    if policy["schema_version"] != POLICY_SCHEMA:
        raise ValueError("unsupported requirements schema")
    _hash(policy["corpus_sha256"], "corpus fingerprint")
    _hash(policy["snapshot_sha256"], "snapshot fingerprint")
    _hash(policy["question_sha256"], "question fingerprint")
    _nonempty(policy["case_id"], "case id", 100)
    if (not isinstance(policy["review_status"], str)
            or policy["review_status"] not in {"pending", "approved"}):
        raise ValueError("invalid review status")
    if (not isinstance(policy["review_basis"], str)
            or policy["review_basis"] not in {"retrospective_regression", "pre_generation"}):
        raise ValueError("invalid review basis")
    _nonempty(policy["reviewer_reference"], "reviewer reference", 200)
    if type(policy["partial_units_allowed"]) is not bool:
        raise ValueError("invalid partial evidence setting")
    units = policy["units"]
    if not isinstance(units, list) or not units or len(units) > MAX_UNITS:
        raise ValueError("invalid policy units")
    seen_units: set[str] = set()
    total_references = 0
    for unit in units:
        _object(unit, {"unit_id", "procedure_scope", "required_groups"}, "unit")
        unit_id = _nonempty(unit["unit_id"], "unit id", 100)
        if unit_id in seen_units:
            raise ValueError("duplicate unit id")
        seen_units.add(unit_id)
        scope = _nonempty(unit["procedure_scope"], "procedure scope", 200)
        groups = unit["required_groups"]
        if not isinstance(groups, list) or not groups or len(groups) > MAX_GROUPS:
            raise ValueError("invalid required groups")
        seen_groups: set[str] = set()
        for group in groups:
            _object(group, {"group_id", "references"}, "requirement group")
            group_id = _nonempty(group["group_id"], "group id", 100)
            if group_id in seen_groups:
                raise ValueError("duplicate group id")
            seen_groups.add(group_id)
            refs = group["references"]
            if not isinstance(refs, list) or not refs or len(refs) > MAX_REFERENCES:
                raise ValueError("invalid alternative references")
            total_references += len(refs)
            if total_references > MAX_POLICY_REFERENCES:
                raise ValueError("policy reference limit exceeded")
            seen_refs: set[tuple[str, str, str, str, str, str]] = set()
            for ref in refs:
                _object(ref, {"law_name", "article", "enforced_date", "source_url",
                              "text_sha256", "procedure_scope"}, "reference")
                for key in ("law_name", "article", "enforced_date", "source_url",
                            "procedure_scope"):
                    _nonempty(ref[key], key, 2000 if key == "source_url" else 200)
                _hash(ref["text_sha256"], "reference text fingerprint")
                if ref["procedure_scope"] != scope:
                    raise ValueError("procedure scope mismatch")
                identity = tuple(ref[key] for key in (
                    "law_name", "article", "enforced_date", "source_url", "text_sha256",
                    "procedure_scope",
                ))
                if identity in seen_refs:
                    raise ValueError("duplicate alternative reference")
                seen_refs.add(identity)
    return policy


def load_corpus(raw: bytes) -> list[dict[str, Any]]:
    records = []
    seen: set[tuple[str, str, str, str]] = set()
    for line in raw.splitlines():
        if not line.strip():
            continue
        if len(records) >= MAX_RECORDS:
            raise ValueError("corpus record limit exceeded")
        row = parse_json(line, "corpus record")
        if not isinstance(row, dict):
            raise ValueError("invalid corpus record")
        for key in ("law_name", "article", "enforced_date", "source_url", "text"):
            _nonempty(row.get(key), f"corpus {key}", 2_000_000 if key == "text" else 2000)
        identity = (row["law_name"], row["article"], row["enforced_date"], row["source_url"])
        if identity in seen:
            raise ValueError("duplicate corpus reference")
        seen.add(identity)
        records.append(row)
    if not records:
        raise ValueError("empty corpus")
    return records


def validate_snapshot(value: Any) -> list[dict[str, Any]]:
    snapshot = _object(value, {"schema_version", "manifest_sha256", "conditions", "sources",
                               "cases", "payload_sha256"}, "snapshot")
    if snapshot["schema_version"] != SNAPSHOT_SCHEMA:
        raise ValueError("unsupported retrieval snapshot schema")
    if (not isinstance(snapshot["conditions"], dict)
            or set(snapshot["conditions"]) != SNAPSHOT_CONDITIONS):
        raise ValueError("invalid snapshot conditions")
    if any(not isinstance(v, str) or not SHA256.fullmatch(v)
           for v in snapshot["conditions"].values()):
        raise ValueError("invalid snapshot condition fingerprint")
    if not isinstance(snapshot["sources"], dict) or set(snapshot["sources"]) != SNAPSHOT_SOURCES:
        raise ValueError("invalid snapshot sources")
    if any(not isinstance(v, str) or not SHA256.fullmatch(v)
           for v in snapshot["sources"].values()):
        raise ValueError("invalid snapshot source fingerprint")
    manifest_hashes = snapshot["manifest_sha256"]
    if (not isinstance(manifest_hashes, list) or not manifest_hashes
            or any(not isinstance(v, str) or not SHA256.fullmatch(v) for v in manifest_hashes)
            or manifest_hashes != sorted(set(manifest_hashes))):
        raise ValueError("invalid snapshot manifest fingerprints")
    payload = {key: snapshot[key] for key in snapshot if key != "payload_sha256"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    if snapshot["payload_sha256"] != digest(canonical):
        raise ValueError("snapshot payload checksum mismatch")
    cases = snapshot["cases"]
    if not isinstance(cases, list) or not cases or len(cases) > MAX_CASES:
        raise ValueError("invalid snapshot cases")
    seen: set[str] = set()
    for case in cases:
        _object(case, {"case_id", "user_input", "chunks"}, "snapshot case")
        case_id = _nonempty(case["case_id"], "snapshot case id", 100)
        if not CASE_ID.fullmatch(case_id):
            raise ValueError("invalid snapshot case id")
        _nonempty(case["user_input"], "snapshot question", 100_000)
        if case_id in seen:
            raise ValueError("duplicate snapshot case id")
        seen.add(case_id)
        chunks = case["chunks"]
        if not isinstance(chunks, list) or not 1 <= len(chunks) <= 3:
            raise ValueError("invalid snapshot chunks")
        for chunk in chunks:
            if not isinstance(chunk, dict) or set(chunk) != {
                "law_name", "article", "enforced_date", "text", "source_url", "score"
            }:
                raise ValueError("invalid snapshot chunk keys")
            for key in ("law_name", "article", "enforced_date", "source_url", "text"):
                _nonempty(chunk.get(key), f"snapshot chunk {key}",
                          2_000_000 if key == "text" else 2000)
            score = chunk["score"]
            if (isinstance(score, bool) or not isinstance(score, numbers.Real)
                    or not math.isfinite(score)):
                raise ValueError("invalid snapshot chunk score")
    if [case["case_id"] for case in cases] != sorted(seen):
        raise ValueError("snapshot cases are not sorted")
    return cases


def verify_case(policy: dict[str, Any], cases: list[dict[str, Any]],
                corpus_hash: str, snapshot_hash: str) -> dict[str, Any]:
    if policy["corpus_sha256"] != corpus_hash:
        raise ValueError("corpus fingerprint mismatch")
    if policy["snapshot_sha256"] != snapshot_hash:
        raise ValueError("snapshot fingerprint mismatch")
    selected = [case for case in cases if case["case_id"] == policy["case_id"]]
    if len(selected) != 1:
        raise ValueError("case id is missing or duplicated")
    case = selected[0]
    if text_digest(case["user_input"]) != policy["question_sha256"]:
        raise ValueError("question fingerprint mismatch")
    return case
