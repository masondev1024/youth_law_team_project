"""Run cloud_rehearsal against a local, connection-caching DynamoDB stub.

This helper is only a subprocess fixture for ``test_http_iam_transport.py``. It
uses the real cloud_rehearsal app, participant store, CloudDeletionAuthority,
botocore HTTP pool, Uvicorn server, and UNIX transport-control socket. The
stub intentionally snapshots the policy state when a TCP connection first
arrives so tests can demonstrate why an explicit client pool close matters.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class _ThreadingServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _write_private(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def run(config_path: Path, policy_path: Path, stats_path: Path, ready_path: Path) -> None:
    import boto3
    import uvicorn
    from botocore.config import Config

    config = json.loads(config_path.read_text(encoding="ascii"))
    stats: dict[str, int] = {
        "connections": 0,
        "requests": 0,
        "stale_allow": 0,
        "stale_deny": 0,
        "deny_responses": 0,
    }
    stats_lock = threading.Lock()

    def policy() -> str:
        value = json.loads(policy_path.read_text(encoding="ascii"))
        decision = value.get("decision")
        if decision not in {"allow", "deny"}:
            raise RuntimeError("invalid local policy fixture")
        return decision

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "LocalDynamoStub"
        sys_version = ""

        def setup(self) -> None:
            super().setup()
            self.policy_at_connect = policy()
            with stats_lock:
                stats["connections"] += 1
                _write_private(stats_path, stats)

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            try:
                json.loads(body)
                target = self.headers.get("X-Amz-Target", "")
                current_policy = policy()
                with stats_lock:
                    stats["requests"] += 1
                    if self.policy_at_connect == "allow" and current_policy == "deny":
                        stats["stale_allow"] += 1
                    if self.policy_at_connect == "deny" and current_policy == "allow":
                        stats["stale_deny"] += 1
                    if self.policy_at_connect == "deny":
                        stats["deny_responses"] += 1
                    _write_private(stats_path, stats)

                if self.policy_at_connect == "deny":
                    self._respond(
                        400,
                        {
                            "__type": "com.amazonaws.dynamodb.v20120810#AccessDeniedException",
                            "message": "Access denied",
                        },
                        error_type="AccessDeniedException",
                    )
                elif target.endswith(".GetItem"):
                    fingerprint = config["dataset_fingerprint"]
                    self._respond(
                        200,
                        {
                            "Item": {
                                "pk": {"S": f"DATASET#{fingerprint}"},
                                "sk": {"S": "CONTROL"},
                                "kind": {"S": "control"},
                                "dataset_fingerprint": {"S": fingerprint},
                                "owner_instance_id": {"S": "i-0123456789abcdef0"},
                                "epoch": {"N": "1"},
                                "highwater": {"N": "0"},
                            }
                        },
                    )
                elif target.endswith(".Query"):
                    self._respond(200, {"Items": [], "Count": 0, "ScannedCount": 0})
                else:
                    self._respond(400, {"message": "Unsupported local fixture operation"})
            except Exception:
                # The fixture emits no request data or exception text.
                self._respond(500, {"message": "Local fixture failure"})

        def _respond(
            self, status: int, payload: dict[str, Any], *, error_type: str | None = None
        ) -> None:
            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/x-amz-json-1.0")
            self.send_header("Content-Length", str(len(raw)))
            if error_type:
                self.send_header("x-amzn-ErrorType", error_type)
            self.end_headers()
            self.wfile.write(raw)

    transport = _ThreadingServer(("127.0.0.1", 0), Handler)
    transport_thread = threading.Thread(target=transport.serve_forever, daemon=True)
    transport_thread.start()
    transport_url = f"http://127.0.0.1:{transport.server_address[1]}"

    def local_aws_client(service: str, region: str):
        if service != "dynamodb":
            # Lambda writer is constructed but never used by this read-only test.
            return object()
        return boto3.client(
            "dynamodb",
            region_name=region,
            endpoint_url=transport_url,
            aws_access_key_id="local-test-key",
            aws_secret_access_key="local-test-secret",
            config=Config(
                connect_timeout=1,
                read_timeout=1,
                retries={"total_max_attempts": 1},
                max_pool_connections=1,
            ),
        )

    import app.cloud_rehearsal as cloud_rehearsal

    cloud_rehearsal.aws_client = local_aws_client
    cloud_rehearsal.instance_identity = lambda: ("i-0123456789abcdef0", "ap-northeast-2")
    cloud_rehearsal.backup_is_fresh = lambda *_args, **_kwargs: True

    def run_uvicorn(application, **options: Any) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        _write_private(
            ready_path,
            {"pid": os.getpid(), "http_port": listener.getsockname()[1]},
        )
        server_config = uvicorn.Config(
            application,
            host="127.0.0.1",
            port=listener.getsockname()[1],
            workers=1,
            access_log=False,
            proxy_headers=False,
            forwarded_allow_ips="",
            log_config=options.get("log_config"),
        )
        uvicorn.Server(server_config).run(sockets=[listener])

    uvicorn.run = run_uvicorn
    try:
        cloud_rehearsal.serve(config)
    finally:
        transport.shutdown()
        transport.server_close()
        transport_thread.join(timeout=1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--stats", type=Path, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.config, args.policy, args.stats, args.ready)
    except Exception as exc:
        # Avoid tracebacks or configuration values in subprocess logs.
        from app.study_storage import StudyStorageError

        code = exc.code if isinstance(exc, StudyStorageError) else type(exc).__name__
        print(json.dumps({"error": code}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
