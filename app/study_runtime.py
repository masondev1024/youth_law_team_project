"""App-owned retention lifecycle and content-free private request logging."""

from __future__ import annotations

import asyncio
import logging
import math
from contextlib import asynccontextmanager, nullcontext
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from fastapi import FastAPI

    from app.participant_storage import ParticipantStudyStore
    from app.study_storage import PrivateStudyStore


class StudyRuntimeUnavailable(RuntimeError):
    """Safe transport error; never includes file names or request contents."""


class PrivateStudyRuntime:
    def __init__(
        self,
        store: PrivateStudyStore,
        *,
        participant_store: ParticipantStudyStore | None = None,
        interval_seconds: float = 60,
    ) -> None:
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or not math.isfinite(interval_seconds)
            or not 0.02 <= interval_seconds <= 3600
        ):
            raise ValueError("invalid cleanup interval")
        self.store = store
        self.participant_store = participant_store
        self.interval_seconds = interval_seconds
        self.healthy = False
        self.cleanup_runs = 0
        self.cleanup_failures = 0
        self.last_deleted = {"observations_deleted": 0, "logs_deleted": 0}
        if participant_store is not None:
            self.last_deleted.update(
                participant_records_deleted=0, participant_revocations_deleted=0
            )

    def _sweep(self) -> None:
        try:
            result = self.store.purge_expired()
            if self.participant_store is not None:
                result.update(self.participant_store.purge_expired())
            if set(result) != set(self.last_deleted) or any(
                type(value) is not int or value < 0 for value in result.values()
            ):
                raise ValueError("invalid cleanup result")
        except Exception:
            self.healthy = False
            self.cleanup_failures += 1
        else:
            self.last_deleted = result
            self.cleanup_runs += 1
            self.healthy = True

    def require_ready(self) -> None:
        if not self.healthy:
            raise StudyRuntimeUnavailable("storage_unavailable")

    def record_request(self, record: dict) -> None:
        self.require_ready()
        try:
            self.store.write_log(record)
        except Exception:
            self.healthy = False
            raise StudyRuntimeUnavailable("storage_unavailable") from None

    def snapshot(self) -> dict:
        return {
            "state": "ready" if self.healthy else "storage_unavailable",
            "cleanup_runs": self.cleanup_runs,
            "cleanup_failures": self.cleanup_failures,
            "last_deleted": dict(self.last_deleted),
            "cleanup_interval_seconds": self.interval_seconds,
            "observation_retention_days": 30,
            "log_retention_hours": 24,
            "capacity_limits": self.store.capacity_limits,
            "session_admission": False,
            "synthetic_observations_only": True,
            "cleanup_while_app_stopped": False,
            **(
                {"participant_record_storage": self.participant_store.snapshot()}
                if self.participant_store is not None
                else {}
            ),
        }

    async def _periodic(self, stopped: asyncio.Event) -> None:
        while not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                await asyncio.to_thread(self._sweep)

    @asynccontextmanager
    async def lifespan(self, _app: FastAPI):
        lease = (
            self.participant_store.service_lease(shared=True)
            if self.participant_store is not None
            else nullcontext()
        )
        # Recovery must fence the whole lifetime, including startup/periodic sweeps.
        with lease:
            await asyncio.to_thread(self._sweep)
            stopped = asyncio.Event()
            task = asyncio.create_task(self._periodic(stopped))
            try:
                yield
            finally:
                stopped.set()
                await task


class PrivateServerLogHandler(logging.Handler):
    """Discard server message/traceback contents; retain categorical metadata."""

    def __init__(self, runtime: PrivateStudyRuntime) -> None:
        super().__init__()
        self.runtime = runtime

    def emit(self, record: logging.LogRecord) -> None:
        exception_class = "UnexpectedError"
        if record.exc_info:
            for allowed in (RuntimeError, ValueError, TypeError, OSError):
                if record.exc_info[0] is allowed:
                    exception_class = allowed.__name__
                    break
        try:
            self.runtime.record_request(
                {
                    "event": "server_diagnostic",
                    "request_id": str(uuid4()),
                    "route": "other",
                    "status": 500 if record.levelno >= logging.ERROR else 200,
                    "duration_ms": 0.0,
                    "exception_class": exception_class,
                }
            )
        except StudyRuntimeUnavailable:
            # Health is degraded. Do not copy the event to an unmanaged sink.
            return


def private_server_log_config(runtime: PrivateStudyRuntime) -> dict:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {
            "private": {"()": lambda: PrivateServerLogHandler(runtime)},
            "null": {"class": "logging.NullHandler"},
        },
        "loggers": {
            "uvicorn": {"handlers": ["private"], "level": "WARNING", "propagate": False},
            "uvicorn.error": {"handlers": ["private"], "level": "WARNING", "propagate": False},
            "uvicorn.access": {"handlers": ["null"], "level": "CRITICAL", "propagate": False},
        },
    }
