"""Explicit private-log entry point; never enables participant sessions."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storage-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("preview", "operator", "rehearsal"), default="preview")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--participant-notice-file", type=Path)
    parser.add_argument("--participant-policy-file", type=Path)
    parser.add_argument("--port", type=int, default=8014)
    args = parser.parse_args(argv)

    from app.protected_study import create_app as protected_app
    from app.protected_study import read_token_file
    from app.study_api import create_app as preview_app
    from app.study_runtime import PrivateStudyRuntime, private_server_log_config
    from app.study_storage import PrivateStudyStore

    try:
        if not 1 <= args.port <= 65535:
            raise ValueError("invalid port")
        if (args.mode in {"operator", "rehearsal"}) != (args.token_file is not None):
            raise ValueError("authenticated mode requires a token file")
        # Validate the credential before creating the managed runtime subtree.
        token = read_token_file(args.token_file) if args.token_file is not None else None
        if (args.participant_notice_file is None) != (args.participant_policy_file is None):
            raise ValueError("both private participant policy files are required")
        participant_store = None
        if args.participant_notice_file is not None:
            from app.participant_policy import load_participant_policy
            from app.participant_storage import ParticipantStudyStore

            digests = load_participant_policy(
                args.storage_root, args.participant_notice_file, args.participant_policy_file
            )
            participant_store = ParticipantStudyStore(args.storage_root, **digests)
        runtime = PrivateStudyRuntime(
            PrivateStudyStore(args.storage_root), participant_store=participant_store
        )
        if args.mode == "rehearsal":
            from app.participant_policy import _private_bytes
            from app.rehearsal_app import create_app as rehearsal_app

            if participant_store is None:
                raise ValueError("rehearsal requires the reviewed private policy release")
            notice_text = _private_bytes(args.storage_root, args.participant_notice_file).decode(
                "utf-8"
            )
            app = rehearsal_app(
                token,
                private_runtime=runtime,
                allowed_origin=f"http://localhost:{args.port}",
                notice_text=notice_text,
            )
        else:
            app = (
                protected_app(token, private_runtime=runtime)
                if token is not None
                else preview_app(runtime)
            )
    except Exception:
        print("private study startup failed", file=sys.stderr)
        return 2

    import uvicorn

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        workers=1,
        access_log=False,
        proxy_headers=False,
        forwarded_allow_ips="",
        log_config=private_server_log_config(runtime),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
