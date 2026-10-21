"""Durable deletion intents kept outside the restorable participant database.

The journal is a local tamper-evident accident guard, not an anti-rollback
security boundary against an owner who can replace both journal and checkpoint.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

from app.study_storage import (
    StudyStorageError,
    _check_ancestors,
    _check_mode,
    _check_no_acl,
)

_NAME = "participant-deletions-v1"
_JOURNAL = "intents.jsonl"
_CHECKPOINT = "checkpoint.json"
_HIGHWATER = "highwater.json"
_MARKER = "marker.json"
_CHECKPOINT_TMP = ".checkpoint.tmp"
_HIGHWATER_TMP = ".highwater.tmp"
_COMPACTION = ".compaction.json"
_COMPACTION_TMP = ".compaction.tmp"
_JOURNAL_TMP = ".journal.tmp"
_ZERO_HASH = "0" * 64
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_BYTES = 32 * 1024 * 1024
_MAX_LINES = 100_000
_ACTIVE_LIMIT = 1000
_SCHEMA = "participant-deletion-ledger.v1"


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


def _canonical_expiry(value: str) -> bool:
    if not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)
    except (TypeError, ValueError):
        return False
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z") == value


class ParticipantDeletionLedger:
    """Hash-chained request-key revocations with an external high-water mark."""

    def __init__(self, root: Path, dataset_fingerprint: str, *, create: bool = False):
        if not isinstance(dataset_fingerprint, str) or not _SHA256.fullmatch(dataset_fingerprint):
            raise StudyStorageError("invalid_configuration")
        self.path = Path(root) / _NAME
        self.journal = self.path / _JOURNAL
        self.checkpoint = self.path / _CHECKPOINT
        self.marker = self.path / _MARKER
        self.dataset_fingerprint = dataset_fingerprint
        if create:
            self._create()
        self._validate()

    @staticmethod
    def fingerprint(marker_data: dict) -> str:
        return hashlib.sha256(_canonical(marker_data)).hexdigest()

    def _validate_file(self, path: Path, *, size: int = 4096) -> os.stat_result:
        try:
            info = path.lstat()
        except OSError as exc:
            raise StudyStorageError("unsafe_ledger") from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > size
        ):
            raise StudyStorageError("unsafe_ledger")
        _check_no_acl(path)
        return info

    def _create(self) -> None:
        try:
            _check_ancestors(self.path.parent)
            if self.path.exists():
                raise StudyStorageError("ledger_exists")
            os.mkdir(self.path, 0o700)
            _check_mode(self.path)
            _check_no_acl(self.path)
            values = {
                "schema": _SCHEMA,
                "owner_uid": os.getuid(),
                "dataset_fingerprint": self.dataset_fingerprint,
            }
            for path, contents in (
                (self.marker, _canonical(values)),
                (self.journal, b""),
                (
                    self.checkpoint,
                    _canonical(
                        {
                            "schema": _SCHEMA,
                            "dataset_fingerprint": self.dataset_fingerprint,
                            "sequence": 0,
                            "entry_hash": _ZERO_HASH,
                            "base_sequence": 0,
                            "base_hash": _ZERO_HASH,
                            "journal_sha256": hashlib.sha256(b"").hexdigest(),
                        }
                    ),
                ),
                (
                    self.path / _HIGHWATER,
                    _canonical(
                        {
                            "schema": _SCHEMA,
                            "dataset_fingerprint": self.dataset_fingerprint,
                            "sequence": 0,
                            "entry_hash": _ZERO_HASH,
                            "journal_sha256": hashlib.sha256(b"").hexdigest(),
                        }
                    ),
                ),
            ):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(contents)
                    stream.flush()
                    os.fsync(stream.fileno())
            self._fsync_dir()
        except StudyStorageError:
            raise
        except OSError as exc:
            raise StudyStorageError("filesystem_error") from exc

    def _fsync_dir(self) -> None:
        fd = os.open(self.path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _validate(self) -> None:
        _check_ancestors(self.path)
        _check_mode(self.path)
        _check_no_acl(self.path)
        try:
            names = {item.name for item in self.path.iterdir()}
        except OSError as exc:
            raise StudyStorageError("unsafe_ledger") from exc
        required = {_JOURNAL, _CHECKPOINT, _HIGHWATER, _MARKER}
        optional = {
            _CHECKPOINT_TMP,
            _HIGHWATER_TMP,
            _COMPACTION,
            _COMPACTION_TMP,
            _JOURNAL_TMP,
        }
        if not required <= names or not names <= required | optional:
            raise StudyStorageError("invalid_ledger")
        self._validate_file(self.marker)
        self._validate_file(self.journal, size=_MAX_BYTES)
        self._validate_file(self.checkpoint)
        self._validate_file(self.path / _HIGHWATER)
        for temporary in optional:
            if (self.path / temporary).exists():
                limit = _MAX_BYTES if temporary == _JOURNAL_TMP else 4096
                self._validate_file(self.path / temporary, size=limit)
        try:
            data = json.loads(self.marker.read_text(encoding="ascii"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise StudyStorageError("invalid_ledger") from exc
        if data != {
            "schema": _SCHEMA,
            "owner_uid": os.getuid(),
            "dataset_fingerprint": self.dataset_fingerprint,
        }:
            raise StudyStorageError("wrong_dataset")

    def verified_entries(self) -> tuple[list[dict], dict]:
        """Validate chain and checkpoint, then publish an anchored complete suffix.

        A crash after fsyncing an intent but before publishing its checkpoint is
        recoverable only when the old checkpoint exactly authenticates the
        journal prefix and every suffix line is complete and hash-valid.
        """
        self._validate()
        raw = self.journal.read_bytes()
        if len(raw) > _MAX_BYTES:
            raise StudyStorageError("ledger_capacity")
        if raw and not raw.endswith(b"\n"):
            raise StudyStorageError("invalid_ledger")
        lines = raw.splitlines()
        if len(lines) > _MAX_LINES:
            raise StudyStorageError("ledger_capacity")
        try:
            checkpoint = json.loads(self.checkpoint.read_text(encoding="ascii"))
            highwater = json.loads(
                (self.path / _HIGHWATER).read_text(encoding="ascii")
            )
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise StudyStorageError("invalid_checkpoint") from exc
        checkpoint, highwater = self._recover_compaction(raw, checkpoint, highwater)
        checkpoint_fields = {
            "schema",
            "dataset_fingerprint",
            "base_sequence",
            "base_hash",
            "sequence",
            "entry_hash",
            "journal_sha256",
        }
        highwater_fields = {
            "schema",
            "dataset_fingerprint",
            "sequence",
            "entry_hash",
            "journal_sha256",
        }
        if type(checkpoint) is not dict or set(checkpoint) != checkpoint_fields:
            raise StudyStorageError("invalid_checkpoint")
        if type(highwater) is not dict or set(highwater) != highwater_fields:
            raise StudyStorageError("invalid_checkpoint")
        if (
            checkpoint.get("schema") != _SCHEMA
            or checkpoint.get("dataset_fingerprint") != self.dataset_fingerprint
            or highwater.get("schema") != _SCHEMA
            or highwater.get("dataset_fingerprint") != self.dataset_fingerprint
        ):
            raise StudyStorageError("wrong_dataset")
        base_sequence = checkpoint.get("base_sequence")
        base_hash = checkpoint.get("base_hash")
        if (
            type(base_sequence) is not int
            or base_sequence < 0
            or not isinstance(base_hash, str)
            or not _SHA256.fullmatch(base_hash)
            or type(checkpoint.get("sequence")) is not int
            or checkpoint["sequence"] < base_sequence
            or type(highwater.get("sequence")) is not int
            or highwater["sequence"] < base_sequence
            or not isinstance(checkpoint.get("entry_hash"), str)
            or not _SHA256.fullmatch(checkpoint["entry_hash"])
            or not isinstance(highwater.get("entry_hash"), str)
            or not _SHA256.fullmatch(highwater["entry_hash"])
            or not isinstance(checkpoint.get("journal_sha256"), str)
            or not _SHA256.fullmatch(checkpoint["journal_sha256"])
            or not isinstance(highwater.get("journal_sha256"), str)
            or not _SHA256.fullmatch(highwater["journal_sha256"])
        ):
            raise StudyStorageError("invalid_checkpoint")
        if highwater["sequence"] > checkpoint["sequence"]:
            raise StudyStorageError("stale_checkpoint")
        entries, sequence, previous, end_offsets = self._parse_journal(
            raw, base_sequence, base_hash
        )

        def chain_state(at_sequence: int) -> tuple[str, int]:
            if at_sequence == base_sequence:
                return base_hash, 0
            index = at_sequence - base_sequence - 1
            if index < 0 or index >= len(entries):
                raise StudyStorageError("stale_checkpoint")
            return entries[index]["entry_hash"], end_offsets[index]

        checkpoint_hash, checkpoint_end = chain_state(checkpoint["sequence"])
        if (
            checkpoint_hash != checkpoint["entry_hash"]
            or hashlib.sha256(raw[:checkpoint_end]).hexdigest()
            != checkpoint["journal_sha256"]
        ):
            raise StudyStorageError("stale_checkpoint")
        highwater_hash, highwater_end = chain_state(highwater["sequence"])
        if highwater_hash != highwater["entry_hash"]:
            raise StudyStorageError("stale_checkpoint")
        if highwater["sequence"] > base_sequence and (
            hashlib.sha256(raw[:highwater_end]).hexdigest()
            != highwater["journal_sha256"]
        ):
            raise StudyStorageError("stale_checkpoint")
        if (
            checkpoint["sequence"] == highwater["sequence"]
            and checkpoint["entry_hash"] != highwater["entry_hash"]
        ):
            raise StudyStorageError("stale_checkpoint")

        # Temporary control files are safe to discard only after both anchors
        # and the complete journal (including any suffix) have been verified.
        self._discard_validated_temporary_files()
        # A complete, valid suffix is a durable deletion intent. Promote it
        # before callers can reconcile or expose records. A partial or
        # unanchored suffix fails above and is never discarded automatically.
        if sequence > checkpoint["sequence"]:
            checkpoint = self._checkpoint_value(
                base_sequence, base_hash, sequence, previous, hashlib.sha256(raw).hexdigest()
            )
            self._publish_checkpoint(checkpoint)
        elif checkpoint["sequence"] == sequence:
            expected_digest = hashlib.sha256(raw).hexdigest()
            if checkpoint["journal_sha256"] != expected_digest:
                raise StudyStorageError("stale_checkpoint")
        if highwater["sequence"] < sequence:
            self._write_highwater(sequence, previous, hashlib.sha256(raw).hexdigest())
        self._discard_compaction_artifacts()
        return entries, checkpoint

    def _parse_journal(
        self, raw: bytes, base_sequence: int, base_hash: str
    ) -> tuple[list[dict], int, str, list[int]]:
        if raw and not raw.endswith(b"\n"):
            raise StudyStorageError("invalid_ledger")
        lines = raw.splitlines()
        if len(lines) > _MAX_LINES:
            raise StudyStorageError("ledger_capacity")
        sequence, previous, entries, end_offsets = base_sequence, base_hash, [], []
        offset = 0
        expected_fields = {
            "sequence",
            "request_key_hash",
            "expires_at",
            "previous_hash",
            "entry_hash",
        }
        for line in lines:
            try:
                entry = json.loads(line.decode("ascii"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise StudyStorageError("invalid_ledger") from exc
            if type(entry) is not dict or set(entry) != expected_fields:
                raise StudyStorageError("invalid_ledger")
            sequence += 1
            core = {key: entry[key] for key in expected_fields - {"entry_hash"}}
            if (
                type(entry["sequence"]) is not int
                or entry["sequence"] != sequence
                or not isinstance(entry["request_key_hash"], str)
                or not _SHA256.fullmatch(entry["request_key_hash"])
                or not isinstance(entry["expires_at"], str)
                or not _canonical_expiry(entry["expires_at"])
                or not isinstance(entry["previous_hash"], str)
                or entry["previous_hash"] != previous
                or not isinstance(entry["entry_hash"], str)
                or not _SHA256.fullmatch(entry["entry_hash"])
                or hashlib.sha256(_canonical(core)).hexdigest() != entry["entry_hash"]
                or _canonical(entry) != line
            ):
                raise StudyStorageError("invalid_ledger")
            previous = entry["entry_hash"]
            entries.append(entry)
            offset += len(line) + 1
            end_offsets.append(offset)
        return entries, sequence, previous, end_offsets

    def _generation_matches(self, raw: bytes, checkpoint: dict) -> bool:
        try:
            entries, sequence, final_hash, _ = self._parse_journal(
                raw, checkpoint["base_sequence"], checkpoint["base_hash"]
            )
        except (KeyError, StudyStorageError):
            return False
        return (
            sequence == checkpoint.get("sequence")
            and final_hash == checkpoint.get("entry_hash")
            and checkpoint.get("journal_sha256") == hashlib.sha256(raw).hexdigest()
            and checkpoint.get("schema") == _SCHEMA
            and checkpoint.get("dataset_fingerprint") == self.dataset_fingerprint
            and len(entries) == sequence - checkpoint["base_sequence"]
        )

    def _recover_compaction(self, raw: bytes, checkpoint: dict, highwater: dict):
        manifest_path = self.path / _COMPACTION
        if not manifest_path.exists():
            return checkpoint, highwater
        try:
            manifest = json.loads(manifest_path.read_text(encoding="ascii"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise StudyStorageError("invalid_ledger") from exc
        if (
            type(manifest) is not dict
            or set(manifest) != {"schema", "dataset_fingerprint", "old", "new"}
            or manifest.get("schema") != _SCHEMA
            or manifest.get("dataset_fingerprint") != self.dataset_fingerprint
            or type(manifest.get("old")) is not dict
            or type(manifest.get("new")) is not dict
        ):
            raise StudyStorageError("invalid_ledger")
        old, new = manifest["old"], manifest["new"]
        if (
            new.get("base_sequence") != old.get("sequence")
            or new.get("base_hash") != old.get("entry_hash")
            or not self._generation_matches(raw, old) and not self._generation_matches(raw, new)
        ):
            raise StudyStorageError("stale_checkpoint")
        highwater_is_old = self._highwater_matches_checkpoint(highwater, old)
        highwater_is_new = self._highwater_matches_checkpoint(highwater, new)
        old_generation = self._generation_matches(raw, old)
        new_generation = self._generation_matches(raw, new)

        if checkpoint == old and old_generation and highwater_is_old:
            # Manifest was durable but replacement was not. Keep the complete
            # old history and retry compaction on the next expiry sweep.
            self._discard_compaction_artifacts(include_manifest=True)
            return checkpoint, highwater
        if checkpoint == old and new_generation and highwater_is_old:
            self._publish_checkpoint(new)
            self._write_highwater(
                new["sequence"], new["entry_hash"], new["journal_sha256"]
            )
            self._discard_compaction_artifacts(include_manifest=True)
            return new, {
                "schema": _SCHEMA,
                "dataset_fingerprint": self.dataset_fingerprint,
                "sequence": new["sequence"],
                "entry_hash": new["entry_hash"],
                "journal_sha256": new["journal_sha256"],
            }
        if checkpoint == new and new_generation and (highwater_is_old or highwater_is_new):
            if highwater_is_old:
                self._write_highwater(
                    new["sequence"], new["entry_hash"], new["journal_sha256"]
                )
            self._discard_compaction_artifacts(include_manifest=True)
            return new, {
                "schema": _SCHEMA,
                "dataset_fingerprint": self.dataset_fingerprint,
                "sequence": new["sequence"],
                "entry_hash": new["entry_hash"],
                "journal_sha256": new["journal_sha256"],
            }
        if checkpoint not in (old, new) or not new_generation:
            raise StudyStorageError("stale_checkpoint")
        raise StudyStorageError("stale_checkpoint")

    def _highwater_matches_checkpoint(self, highwater: dict, checkpoint: dict) -> bool:
        return (
            type(highwater) is dict
            and highwater.get("schema") == _SCHEMA
            and highwater.get("dataset_fingerprint") == self.dataset_fingerprint
            and highwater.get("sequence") == checkpoint.get("sequence")
            and highwater.get("entry_hash") == checkpoint.get("entry_hash")
            and highwater.get("journal_sha256") == checkpoint.get("journal_sha256")
        )

    def _discard_compaction_artifacts(self, *, include_manifest: bool = False) -> None:
        names = [_COMPACTION_TMP, _JOURNAL_TMP]
        if include_manifest:
            names.append(_COMPACTION)
        changed = False
        for name in names:
            path = self.path / name
            if path.exists():
                self._validate_file(path, size=_MAX_BYTES if name == _JOURNAL_TMP else 4096)
                path.unlink()
                changed = True
        if changed:
            self._fsync_dir()

    def _checkpoint_value(
        self, base_sequence: int, base_hash: str, sequence: int, entry_hash: str, digest: str
    ) -> dict:
        return {
            "schema": _SCHEMA,
            "dataset_fingerprint": self.dataset_fingerprint,
            "base_sequence": base_sequence,
            "base_hash": base_hash,
            "sequence": sequence,
            "entry_hash": entry_hash,
            "journal_sha256": digest,
        }

    def _write_json_atomic(self, name: str, temporary_name: str, payload: dict) -> None:
        temporary = self.path / temporary_name
        target = self.path / name
        if temporary.exists():
            raise StudyStorageError("unsafe_ledger")
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(_canonical(payload))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            self._fsync_dir()
        except OSError as exc:
            raise StudyStorageError("filesystem_error") from exc

    def _publish_checkpoint(self, checkpoint: dict) -> None:
        self._write_json_atomic(_CHECKPOINT, _CHECKPOINT_TMP, checkpoint)

    def _write_highwater(self, sequence: int, entry_hash: str, digest: str) -> None:
        self._write_json_atomic(
            _HIGHWATER,
            _HIGHWATER_TMP,
            {
                "schema": _SCHEMA,
                "dataset_fingerprint": self.dataset_fingerprint,
                "sequence": sequence,
                "entry_hash": entry_hash,
                "journal_sha256": digest,
            },
        )

    def _discard_validated_temporary_files(self) -> None:
        changed = False
        for name in (_CHECKPOINT_TMP, _HIGHWATER_TMP):
            temporary = self.path / name
            if temporary.exists():
                self._validate_file(temporary)
                temporary.unlink()
                changed = True
        if changed:
            self._fsync_dir()

    def active_entries(self, now: datetime) -> list[dict]:
        entries, _ = self.verified_entries()
        active: dict[str, dict] = {}
        for entry in entries:
            if entry["expires_at"] > now.isoformat(timespec="microseconds").replace("+00:00", "Z"):
                active[entry["request_key_hash"]] = entry
        return list(active.values())

    def append(self, request_key_hash: str, expires_at: str, now: datetime) -> dict:
        if not isinstance(request_key_hash, str) or not _SHA256.fullmatch(request_key_hash):
            raise StudyStorageError("invalid_record")
        if not _canonical_expiry(expires_at):
            raise StudyStorageError("invalid_record")
        entries, checkpoint = self.verified_entries()
        active_hashes = {
            entry["request_key_hash"]
            for entry in entries
            if entry["expires_at"] > now.isoformat(timespec="microseconds").replace("+00:00", "Z")
        }
        if request_key_hash not in active_hashes and len(active_hashes) >= _ACTIVE_LIMIT:
            raise StudyStorageError("ledger_capacity")
        previous_entry = next(
            (entry for entry in reversed(entries) if entry["request_key_hash"] == request_key_hash),
            None,
        )
        if previous_entry is not None:
            if previous_entry["expires_at"] != expires_at:
                raise StudyStorageError("invalid_ledger")
            if request_key_hash in active_hashes:
                return previous_entry
        core = {
            "sequence": checkpoint["sequence"] + 1,
            "request_key_hash": request_key_hash,
            "expires_at": expires_at,
            "previous_hash": checkpoint["entry_hash"],
        }
        entry = {**core, "entry_hash": hashlib.sha256(_canonical(core)).hexdigest()}
        line = _canonical(entry) + b"\n"
        info = self._validate_file(self.journal, size=_MAX_BYTES)
        if info.st_size + len(line) > _MAX_BYTES or len(entries) >= _MAX_LINES:
            raise StudyStorageError("ledger_capacity")
        fd = os.open(self.journal, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
        try:
            offset = 0
            while offset < len(line):
                written = os.write(fd, line[offset:])
                if written <= 0:
                    raise OSError("short ledger write")
                offset += written
            os.fsync(fd)
        except OSError as exc:
            raise StudyStorageError("filesystem_error") from exc
        finally:
            os.close(fd)
        raw = self.journal.read_bytes()
        self._write_checkpoint(
            checkpoint["base_sequence"],
            checkpoint["base_hash"],
            entry["sequence"],
            entry["entry_hash"],
            hashlib.sha256(raw).hexdigest(),
        )
        return entry

    def _write_checkpoint(
        self,
        base_sequence: int,
        base_hash: str,
        sequence: int,
        entry_hash: str,
        journal_sha256: str,
    ) -> None:
        checkpoint = self._checkpoint_value(
            base_sequence, base_hash, sequence, entry_hash, journal_sha256
        )
        self._publish_checkpoint(checkpoint)
        self._write_highwater(sequence, entry_hash, journal_sha256)

    def seed(self, revocations: list[tuple[str, str]], now: datetime) -> None:
        entries, _ = self.verified_entries()
        existing = {entry["request_key_hash"] for entry in entries}
        for request_hash, expires_at in revocations:
            if (
                expires_at > self._iso(now)
                and request_hash not in existing
            ):
                self.append(request_hash, expires_at, now)
                existing.add(request_hash)

    def compact(self, now: datetime) -> None:
        """Rotate expired hashes while preserving a recoverable publication intent."""
        entries, checkpoint = self.verified_entries()
        active = [item for item in entries if item["expires_at"] > self._iso(now)]
        if len(active) == len(entries):
            return
        base_sequence = checkpoint["sequence"]
        base_hash = checkpoint["entry_hash"]
        sequence, previous, retained = base_sequence, base_hash, []
        for old_entry in active:
            sequence += 1
            core = {
                "sequence": sequence,
                "request_key_hash": old_entry["request_key_hash"],
                "expires_at": old_entry["expires_at"],
                "previous_hash": previous,
            }
            entry = {**core, "entry_hash": hashlib.sha256(_canonical(core)).hexdigest()}
            retained.append(entry)
            previous = entry["entry_hash"]
        raw = b"".join(_canonical(entry) + b"\n" for entry in retained)
        if raw == self.journal.read_bytes():
            return
        new_checkpoint = self._checkpoint_value(
            base_sequence,
            base_hash,
            sequence,
            previous,
            hashlib.sha256(raw).hexdigest(),
        )
        journal_tmp = self.path / _JOURNAL_TMP
        try:
            if journal_tmp.exists() or (self.path / _COMPACTION).exists():
                raise StudyStorageError("unsafe_ledger")
            fd = os.open(
                journal_tmp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            manifest = {
                "schema": _SCHEMA,
                "dataset_fingerprint": self.dataset_fingerprint,
                "old": checkpoint,
                "new": new_checkpoint,
            }
            self._write_json_atomic(_COMPACTION, _COMPACTION_TMP, manifest)
            os.replace(journal_tmp, self.journal)
            self._fsync_dir()
            self._write_checkpoint(
                base_sequence,
                base_hash,
                sequence,
                previous,
                hashlib.sha256(raw).hexdigest(),
            )
            (self.path / _COMPACTION).unlink()
            self._fsync_dir()
        except StudyStorageError:
            raise
        except OSError as exc:
            raise StudyStorageError("filesystem_error") from exc

    @staticmethod
    def _iso(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise StudyStorageError("invalid_clock")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )

    def checkpoint_id(self) -> str:
        _, checkpoint = self.verified_entries()
        return f"{checkpoint['sequence']}:{checkpoint['entry_hash']}"

    def require_checkpoint(self, expected: str) -> dict:
        _, checkpoint = self.verified_entries()
        actual = f"{checkpoint['sequence']}:{checkpoint['entry_hash']}"
        if not isinstance(expected, str) or expected != actual:
            raise StudyStorageError("stale_checkpoint")
        return checkpoint
