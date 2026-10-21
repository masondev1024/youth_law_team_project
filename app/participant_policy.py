"""Read private, versioned operator policy files without publishing their contents."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from app.participant_models import NOTICE_VERSION, POLICY_VERSION
from app.study_storage import _check_ancestors, _check_mode, _check_no_acl

# Content fingerprints of the local r2 rehearsal release; no contact details are
# embedded in source. A new copy needs a new reviewed release and version.
REHEARSAL_NOTICE_SHA256 = "a8542327f9a136abd52e479300caa60208c0066fc4f057bea03129224df07cf9"
REHEARSAL_POLICY_SHA256 = "75ba5bf3fbc4a40fb1946e6a8346734823697e202b7549ad474959b074a6bd62"


def _private_bytes(root: Path, path: Path) -> bytes:
    if not path.is_absolute() or not path.is_relative_to(root / "control"):
        raise ValueError("invalid private policy path")
    _check_ancestors(path)
    _check_mode(root / "control")
    _check_no_acl(root / "control")
    info = _check_mode(path, 0o600)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
        raise ValueError("invalid private policy file")
    _check_no_acl(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("private policy changed during read")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(65537)
    finally:
        os.close(fd)
    if len(data) > 65536:
        raise ValueError("invalid private policy size")
    return data


def load_participant_policy(root: Path, notice_file: Path, policy_file: Path) -> dict[str, str]:
    """Return server-pinned digests; never return contact/payment details to logs."""
    notice = _private_bytes(root, notice_file)
    policy = _private_bytes(root, policy_file)
    notice_digest = hashlib.sha256(notice).hexdigest()
    policy_digest = hashlib.sha256(policy).hexdigest()
    if (notice_digest, policy_digest) != (REHEARSAL_NOTICE_SHA256, REHEARSAL_POLICY_SHA256):
        raise ValueError("unrecognized rehearsal release content")
    if NOTICE_VERSION not in notice.decode("utf-8"):
        raise ValueError("unrecognized notice version")
    data = json.loads(policy)
    if data.get("versions") != {"notice": NOTICE_VERSION, "policy": POLICY_VERSION}:
        raise ValueError("unrecognized policy version")
    scope = data.get("scope_and_admission", {})
    if (
        scope.get("origin") != "developer_rehearsal"
        or scope.get("real_participant_admission") is not False
        or scope.get("recruitment_authorized") is not False
    ):
        raise ValueError("participant admission must remain closed")
    return {
        "notice_sha256": notice_digest,
        "policy_sha256": policy_digest,
    }
