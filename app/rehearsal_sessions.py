"""Bounded, process-local browser sessions over the private rehearsal store."""

from __future__ import annotations

import hmac
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from typing import Iterator
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from app.participant_models import NOTICE_VERSION, POLICY_VERSION, ConsentInput, ObservationInput
from app.participant_storage import ParticipantStudyStore

SESSION_SECONDS = 1800
MAX_SESSIONS = 32


class RehearsalError(Exception):
    def __init__(self, code: str, status: int):
        self.code = code
        self.status = status


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LoginInput(StrictInput):
    token: str = Field(min_length=1, max_length=4096)


class ConsentRequest(StrictInput):
    task_id: str
    consent: ConsentInput


class RestoreRequest(StrictInput):
    record_id: str = Field(min_length=36, max_length=36)


class TimingRequest(StrictInput):
    elapsed_seconds: StrictInt = Field(ge=0, le=360)
    card_displayed: StrictBool


class ObservationRequest(StrictInput):
    expected_version: StrictInt = Field(ge=1)
    observation: ObservationInput


@dataclass
class BrowserSession:
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    expires: float = field(default_factory=lambda: time.monotonic() + SESSION_SECONDS)
    record_id: str | None = None
    request_key: str | None = None
    started: bool = False
    timing_seconds: int | None = None
    card_displayed: bool = False
    last_action: str | None = None
    lock: Lock = field(default_factory=Lock)

    def clear_workflow(self) -> None:
        self.started = False
        self.timing_seconds = None
        self.card_displayed = False


class RehearsalSessions:
    def __init__(
        self, token: str, store: ParticipantStudyStore, notice_text: str, tasks: list[dict]
    ):
        self.expected = token.encode("utf-8")
        self.store = store
        self.notice_text = notice_text
        self.tasks = tasks
        self.task_ids = {task["id"] for task in tasks}
        self.sessions: dict[str, BrowserSession] = {}
        self.lock = Lock()
        self.failed_logins: list[float] = []

    def login(self, token: str) -> tuple[str, BrowserSession]:
        with self.lock:
            now = time.monotonic()
            self.failed_logins = [stamp for stamp in self.failed_logins if stamp > now - 60]
            if len(self.failed_logins) >= 10:
                raise RehearsalError("login_rate_limited", 429)
            if not hmac.compare_digest(token.encode("utf-8"), self.expected):
                self.failed_logins.append(now)
                raise RehearsalError("unauthorized", 401)
            self.sessions = {
                key: value for key, value in self.sessions.items() if value.expires > now
            }
            if len(self.sessions) >= MAX_SESSIONS:
                # Do not evict a live session's association with a stored record.
                raise RehearsalError("busy", 429)
            sid = secrets.token_urlsafe(32)
            session = BrowserSession()
            self.sessions[sid] = session
            return sid, session

    def find(self, sid: str | None) -> BrowserSession:
        with self.lock:
            session = self.sessions.get(sid or "")
            if session is None or session.expires <= time.monotonic():
                self.sessions.pop(sid or "", None)
                raise RehearsalError("unauthorized", 401)
            return session

    def logout(self, sid: str) -> None:
        with self.lock:
            self.sessions.pop(sid, None)

    @contextmanager
    def locked(self, session: BrowserSession) -> Iterator[None]:
        if not session.lock.acquire(timeout=2):
            raise RehearsalError("busy", 429)
        try:
            if session.expires <= time.monotonic():
                raise RehearsalError("unauthorized", 401)
            yield
        finally:
            session.lock.release()

    def record(self, session: BrowserSession) -> dict:
        if session.record_id is None:
            raise RehearsalError("consent_required", 409)
        record = self.store.read_record(session.record_id)
        if record is None:
            session.record_id = None
            session.request_key = None
            session.clear_workflow()
            session.last_action = "expired"
            raise RehearsalError("expired", 410)
        return record

    def state(self, session: BrowserSession) -> dict:
        record = None
        if session.record_id is not None:
            try:
                record = self.record(session)
            except RehearsalError as exc:
                if exc.code != "expired":
                    raise
        revealed_task_id = (
            record["task_id"]
            if record is not None and (session.started or record["observation"] is not None)
            else None
        )
        return {
            "csrf_token": session.csrf_token,
            "session_admission": False,
            "record_origin": "developer_rehearsal",
            "notice": {
                "text": self.notice_text,
                "notice_version": NOTICE_VERSION,
                "policy_version": POLICY_VERSION,
            },
            "tasks": [
                {**task, "scenario": task["scenario"] if task["id"] == revealed_task_id else ""}
                for task in self.tasks
            ],
            "record": record,
            "available_records": self.store.list_records(),
            "workflow": {
                "started": session.started,
                "timing_seconds": session.timing_seconds,
                "card_displayed": session.card_displayed,
            },
            "last_action": session.last_action,
        }

    def apply(self, session: BrowserSession, action: str, payload: StrictInput) -> dict:
        """Commit and update the association in one non-cancellable thread operation."""
        with self.locked(session):
            if action == "consent":
                assert isinstance(payload, ConsentRequest)
                self._consent(session, payload)
            elif action == "restore":
                assert isinstance(payload, RestoreRequest)
                if session.record_id and session.record_id != payload.record_id:
                    raise RehearsalError("conflict", 409)
                record = self.store.read_record(payload.record_id)
                if record is None:
                    raise RehearsalError("expired", 410)
                session.record_id = record["record_id"]
                session.clear_workflow()
                # No process-local timer survives re-authentication or restart.
                # An unsaved restored task is conservatively treated as interrupted.
                session.started = record["observation"] is None
                session.last_action = "restored"
            elif action == "start":
                record = self.record(session)
                if record["observation"] is not None:
                    raise RehearsalError("conflict", 409)
                # Ambiguous response retries must not erase a stopped checkpoint.
                session.started = True
                session.last_action = "started"
            elif action == "restart":
                record = self.record(session)
                if (
                    record["observation"] is not None
                    or not session.started
                    or session.timing_seconds is not None
                ):
                    raise RehearsalError("conflict", 409)
                session.clear_workflow()
                session.started = True
                session.last_action = "restarted"
            elif action == "timing":
                assert isinstance(payload, TimingRequest)
                self.record(session)
                if not session.started:
                    raise RehearsalError("task_not_started", 409)
                # A stop checkpoint is stable on retry. Restart must be explicit.
                if session.timing_seconds is not None and (
                    session.timing_seconds != payload.elapsed_seconds
                    or session.card_displayed != payload.card_displayed
                ):
                    raise RehearsalError("conflict", 409)
                session.timing_seconds = payload.elapsed_seconds
                session.card_displayed = payload.card_displayed
                session.last_action = "task_stopped"
            elif action == "observations":
                assert isinstance(payload, ObservationRequest)
                record = self.record(session)
                self.store.write_observation(
                    record["record_id"],
                    payload.observation.model_dump(),
                    expected_version=payload.expected_version,
                )
                session.last_action = "observation_saved"
            elif action == "withdraw":
                if session.record_id is not None:
                    self.store.withdraw(session.record_id)
                session.record_id = None
                session.request_key = None
                session.clear_workflow()
                session.last_action = "withdrawn"
            else:
                raise RehearsalError("not_found", 404)
            return self.state(session)

    def _consent(self, session: BrowserSession, payload: ConsentRequest) -> None:
        if payload.task_id not in self.task_ids:
            raise RehearsalError("invalid_record", 422)
        if session.record_id is None and (
            not payload.consent.participation_agreed
            or not payload.consent.observation_recording_agreed
        ):
            # Refusal must work without affirming adulthood and creates no row or key.
            session.request_key = None
            session.last_action = "declined"
            return
        if session.record_id is not None:
            record = self.record(session)
            if (
                record["task_id"] != payload.task_id
                or record["consent"] != payload.consent.model_dump()
            ):
                raise RehearsalError("conflict", 409)
        else:
            if session.request_key is None:
                session.request_key = f"{int(self.store.clock().timestamp())}:{uuid4()}"
            record = self.store.create_consent(
                payload.consent.model_dump(),
                task_id=payload.task_id,
                request_key=session.request_key,
            )
            if record is not None:
                session.record_id = record["record_id"]
            else:
                session.request_key = None
        session.last_action = "consented" if session.record_id is not None else "declined"
