"""Offline source-evidence checker for the fixed preparation catalog.

This module creates an internal source bundle only when saved response bytes
match the catalog's recorded digests. It never fetches sources or promotes an
artifact into the running application.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any

from app.preparation_service import _catalog_fingerprint, _load_catalog, _today

_MANIFEST_SCHEMA = "preparation-source-evidence.v1"
_MAX_CATALOG_BYTES = 2 * 1024 * 1024
_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_CAPTURE_BYTES = 5 * 1024 * 1024
_MAX_SOURCE_COUNT = 32
_MAX_TOTAL_CAPTURE_BYTES = 10 * 1024 * 1024
_MAX_AGE_DAYS = 30
_SOURCE_FIELDS = {
    "id",
    "url",
    "capture_path",
    "checked_on",
    "review_kind",
    "reviewer_reference",
}
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_SOURCE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")


class ReleaseInputError(Exception):
    """A safe, content-free failure code for malformed or inaccessible input."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _load_json_bytes(path: Path, maximum: int, code: str) -> tuple[bytes, Any]:
    raw = _read_bounded(path, maximum, code)
    try:
        return raw, json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ReleaseInputError(code) from None


def _read_bounded(path: Path, maximum: int, code: str) -> bytes:
    try:
        with path.open("rb") as stream:
            raw = stream.read(maximum + 1)
    except OSError:
        raise ReleaseInputError(code) from None
    if len(raw) > maximum:
        raise ReleaseInputError(code)
    return raw


def _safe_capture_path(root: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    target = root
    try:
        for part in relative.parts:
            target = target / part
            if target.is_symlink():
                return None
        resolved_root = root.resolve(strict=True)
        resolved = target.resolve(strict=True)
        resolved.relative_to(resolved_root)
        if not resolved.is_file():
            return None
        return resolved
    except (OSError, RuntimeError, ValueError):
        return None


def _output_overlaps_inputs(output: Path, protected_dirs: set[Path]) -> bool:
    candidate = output.parent.resolve() / output.name
    for directory in protected_dirs:
        try:
            directory = directory.resolve(strict=True)
        except OSError:
            continue
        if (
            candidate == directory
            or candidate in directory.parents
            or directory in candidate.parents
        ):
            return True
    return False


def _outputs_overlap(first: Path, second: Path) -> bool:
    first_path = first.resolve()
    second_path = second.resolve()
    return (
        first_path == second_path
        or first_path in second_path.parents
        or second_path in first_path.parents
    )


def _manifest_sources(
    manifest: Any,
    catalog: dict[str, Any],
    catalog_hash: str,
    evidence_root: Path | None,
    today: date,
) -> tuple[list[dict[str, Any]], dict[str, bytes], list[str]]:
    issues: list[str] = []
    captures: dict[str, bytes] = {}
    if not isinstance(manifest, dict) or set(manifest) != {
        "schema_version",
        "catalog_sha256",
        "sources",
    }:
        return [], captures, ["invalid_manifest_schema"]
    if manifest.get("schema_version") != _MANIFEST_SCHEMA:
        issues.append("unsupported_manifest_schema")
    if manifest.get("catalog_sha256") != catalog_hash:
        issues.append("catalog_digest_mismatch")
    expected = {source["id"]: source for source in catalog["sources"]}
    supplied = manifest.get("sources")
    if not isinstance(supplied, list):
        return [], captures, issues + ["invalid_source_list"]
    if len(supplied) > _MAX_SOURCE_COUNT:
        return [], captures, issues + ["too_many_sources"]

    seen: set[str] = set()
    accepted: list[dict[str, Any]] = []
    total_capture_bytes = 0
    for record in supplied:
        if not isinstance(record, dict) or set(record) != _SOURCE_FIELDS:
            issues.append("invalid_source_metadata")
            continue
        source_id = record.get("id")
        if not isinstance(source_id, str) or source_id not in expected:
            issues.append("unknown_source")
            continue
        if source_id in seen:
            issues.append("duplicate_source")
            continue
        seen.add(source_id)
        source = expected[source_id]
        if record.get("url") != source.get("url"):
            issues.append("source_url_mismatch")
        if record.get("review_kind") != "official_page_check":
            issues.append("invalid_review_kind")
        checked_on = record.get("checked_on")
        try:
            if not isinstance(checked_on, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", checked_on
            ):
                raise ValueError
            reviewed_date = date.fromisoformat(checked_on)
            age = (today - reviewed_date).days
            if age < 0:
                issues.append("future_review_date")
            elif age > _MAX_AGE_DAYS:
                issues.append("expired_review_date")
            if checked_on != source.get("checked_on"):
                issues.append("source_date_mismatch")
        except ValueError:
            issues.append("invalid_review_date")
        reviewer_reference = record.get("reviewer_reference")
        if (
            not isinstance(reviewer_reference, str)
            or not reviewer_reference.strip()
            or len(reviewer_reference) > 500
        ):
            issues.append("missing_reviewer_reference")

        if evidence_root is None:
            issues.append("missing_capture")
            continue
        capture_path = _safe_capture_path(evidence_root, record.get("capture_path"))
        if capture_path is None:
            issues.append("invalid_capture_path")
            continue
        try:
            content = _read_bounded(
                capture_path, _MAX_CAPTURE_BYTES, "capture_too_large"
            )
        except ReleaseInputError as error:
            issues.append(error.code)
            continue
        if total_capture_bytes + len(content) > _MAX_TOTAL_CAPTURE_BYTES:
            issues.append("aggregate_capture_too_large")
            continue
        expected_digest = source.get("retrieved_body_sha256")
        if not isinstance(expected_digest, str) or not _HEX_DIGEST.fullmatch(expected_digest):
            issues.append("invalid_catalog_body_digest")
            continue
        digest = _sha256(content)
        if digest != expected_digest:
            issues.append("capture_digest_mismatch")
            continue
        total_capture_bytes += len(content)
        captures[source_id] = content
        accepted.append(
            {
                "id": source_id,
                "url": source["url"],
                "checked_on": checked_on,
                "review_kind": "official_page_check",
                "reviewer_reference": reviewer_reference.strip(),
                "capture_sha256": digest,
            }
        )

    if seen != set(expected):
        issues.append("missing_source_metadata")
    # Stable and de-duplicated codes keep reports safe and easy to automate.
    return accepted, captures, sorted(set(issues))


def _read_and_validate(
    catalog_path: Path,
    evidence_manifest_path: Path | None,
) -> tuple[
    bytes,
    dict[str, Any],
    str,
    list[dict[str, Any]],
    dict[str, bytes],
    list[str],
    Path | None,
    bytes | None,
]:
    catalog_bytes, _ = _load_json_bytes(
        catalog_path, _MAX_CATALOG_BYTES, "invalid_catalog"
    )
    try:
        catalog = _load_catalog(path=catalog_path, raw_bytes=catalog_bytes)
    except Exception:
        raise ReleaseInputError("invalid_catalog") from None
    if any(
        not _SAFE_SOURCE_ID.fullmatch(source["id"])
        for source in catalog["sources"]
    ):
        raise ReleaseInputError("invalid_catalog")
    catalog_hash = _catalog_fingerprint(catalog)
    if evidence_manifest_path is None:
        return (
            catalog_bytes,
            catalog,
            catalog_hash,
            [],
            {},
            ["missing_evidence_manifest", "missing_source_metadata"],
            None,
            None,
        )
    manifest_bytes, manifest = _load_json_bytes(
        evidence_manifest_path, _MAX_MANIFEST_BYTES, "invalid_evidence_manifest"
    )
    root = evidence_manifest_path.parent
    accepted, captures, issues = _manifest_sources(
        manifest, catalog, catalog_hash, root, _today()
    )
    return catalog_bytes, catalog, catalog_hash, accepted, captures, issues, root, manifest_bytes


def _report(
    status: str,
    catalog_hash: str | None,
    sources: list[dict[str, Any]],
    issues: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": "preparation-release-report.v1",
        "status": status,
        "readiness_scope": "internal_source_bundle",
        "catalog_sha256": catalog_hash,
        "sources": [
            {
                "id": item["id"],
                "capture_sha256": item["capture_sha256"],
            }
            for item in sources
        ],
        "issue_codes": sorted(set(issues)),
        "public_release_approval": "not_established",
        "participant_research_approval": "not_established",
        "limitations": [
            "Input ceilings are 32 sources, 5 MiB per capture, and 10 MiB total capture bytes.",
            "A matching digest confirms bytes only, not source authenticity, applicability, "
            "or expert approval.",
            "A reviewer reference is an operator record, not proof of human or expert review.",
        ],
    }


def _reserve_output(path: Path) -> int:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ReleaseInputError("output_collision") from None
    except OSError:
        raise ReleaseInputError("output_io_error") from None


def _write_reserved_output(file_descriptor: int, content: bytes) -> None:
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise ReleaseInputError("output_io_error") from None


def _report_bytes(document: dict[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True, indent=2).encode("utf-8") + b"\n"


def _create_bundle(
    bundle_path: Path,
    catalog_path: Path,
    catalog_bytes: bytes,
    catalog_hash: str,
    sources: list[dict[str, Any]],
    captures: dict[str, bytes],
    manifest_path: Path,
    manifest_bytes: bytes,
) -> None:
    try:
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        bundle_path.mkdir(mode=0o700, parents=False, exist_ok=False)
    except FileExistsError:
        raise ReleaseInputError("output_collision") from None
    except OSError:
        raise ReleaseInputError("output_io_error") from None
    try:
        if (
            _read_bounded(catalog_path, _MAX_CATALOG_BYTES, "catalog_changed_during_release")
            != catalog_bytes
        ):
            raise ReleaseInputError("catalog_changed_during_release")
        files: dict[str, bytes] = {"catalog.json": catalog_bytes}
        evidence_sources = []
        for index, source in enumerate(sources):
            source_id = source["id"]
            capture_path = f"captures/source-{index:03d}.bin"
            files[capture_path] = captures[source_id]
            evidence_sources.append(
                {
                    key: source[key]
                    for key in (
                        "id",
                        "url",
                        "checked_on",
                        "review_kind",
                        "reviewer_reference",
                    )
                }
                | {"capture_path": capture_path}
            )
        evidence_manifest = {
            "schema_version": _MANIFEST_SCHEMA,
            "catalog_sha256": catalog_hash,
            "sources": evidence_sources,
        }
        files["evidence-manifest.json"] = json.dumps(
            evidence_manifest, ensure_ascii=False, sort_keys=True, indent=2
        ).encode("utf-8") + b"\n"

        current_manifest = _read_bounded(
            manifest_path, _MAX_MANIFEST_BYTES, "evidence_changed_during_release"
        )
        if current_manifest != manifest_bytes:
            raise ReleaseInputError("evidence_changed_during_release")
        # Re-read every captured object immediately before writing the marker.
        _, manifest = _load_json_bytes(
            manifest_path, _MAX_MANIFEST_BYTES, "evidence_changed_during_release"
        )
        _, current_captures, issues = _manifest_sources(
            manifest, _load_catalog(path=catalog_path, raw_bytes=catalog_bytes),
            catalog_hash, manifest_path.parent, _today()
        )
        if issues or current_captures != captures:
            raise ReleaseInputError("evidence_changed_during_release")

        hashes: dict[str, str] = {}
        for relative, content in files.items():
            destination = bundle_path / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor = _reserve_output(destination)
            _write_reserved_output(descriptor, content)
            hashes[relative] = _sha256(content)
        if (
            _read_bounded(catalog_path, _MAX_CATALOG_BYTES, "catalog_changed_during_release")
            != catalog_bytes
        ):
            raise ReleaseInputError("catalog_changed_during_release")
        completion = {
            "schema_version": "preparation-release-completion.v1",
            "status": "complete",
            "readiness_scope": "internal_source_bundle",
            "catalog_sha256": catalog_hash,
            "files_sha256": dict(sorted(hashes.items())),
            "public_release_approval": "not_established",
            "participant_research_approval": "not_established",
        }
        marker = json.dumps(completion, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        descriptor = _reserve_output(bundle_path / "completion.json")
        _write_reserved_output(descriptor, marker)
    except Exception:
        shutil.rmtree(bundle_path, ignore_errors=True)
        raise


def build_release(
    catalog_path: Path,
    evidence_manifest_path: Path | None,
    report_path: Path,
    bundle_path: Path | None = None,
) -> tuple[int, dict[str, Any]]:
    """Validate saved evidence and optionally create a complete internal bundle."""
    protected = {catalog_path.parent}
    if evidence_manifest_path is not None:
        protected.add(evidence_manifest_path.parent)
    if _output_overlaps_inputs(report_path, protected) or (
        bundle_path is not None and _output_overlaps_inputs(bundle_path, protected)
    ):
        raise ReleaseInputError("output_overlaps_inputs")
    if bundle_path is not None and _outputs_overlap(report_path, bundle_path):
        raise ReleaseInputError("output_collision")

    # Reserve first so an existing report can never leave behind a new bundle.
    report_fd = _reserve_output(report_path)
    try:
        (
            catalog_bytes,
            catalog,
            catalog_hash,
            sources,
            captures,
            issues,
            root,
            manifest_bytes,
        ) = _read_and_validate(catalog_path, evidence_manifest_path)
    except ReleaseInputError as error:
        document = _report("error", None, [], [error.code])
        _write_reserved_output(report_fd, _report_bytes(document))
        return 1, document

    status = "complete" if not issues else "blocked"
    if status == "complete" and bundle_path is not None:
        assert evidence_manifest_path is not None
        try:
            _create_bundle(
                bundle_path,
                catalog_path,
                catalog_bytes,
                catalog_hash,
                sources,
                captures,
                evidence_manifest_path,
                manifest_bytes or b"",
            )
        except ReleaseInputError as error:
            document = _report("error", catalog_hash, sources, [error.code])
            _write_reserved_output(report_fd, _report_bytes(document))
            return 1, document
        except Exception:
            document = _report("error", catalog_hash, sources, ["output_io_error"])
            _write_reserved_output(report_fd, _report_bytes(document))
            return 1, document
    document = _report(status, catalog_hash, sources, issues)
    _write_reserved_output(report_fd, _report_bytes(document))
    return (0 if status == "complete" else 2), document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--evidence-manifest", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--bundle", type=Path)
    args = parser.parse_args(argv)
    try:
        exit_code, document = build_release(
            args.catalog, args.evidence_manifest, args.report, args.bundle
        )
    except ReleaseInputError as error:
        print(json.dumps({"status": "error", "issue_code": error.code}), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"status": "error", "issue_code": "io_error"}), file=sys.stderr)
        return 1
    print(json.dumps({"status": document["status"]}))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
