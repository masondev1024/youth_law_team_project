"""Contracts for the deterministic first-consultation preparation card."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

Issue = Literal["repayment", "collection", "procedure"]


class PreparationCardRequest(BaseModel):
    """Only accepts catalog identifiers; free text and extra keys are forbidden."""

    model_config = ConfigDict(extra="forbid", strict=True)

    issue: Issue
    selected_facts: list[StrictStr] = Field(max_length=12)


class PreparationError(Exception):
    """Safe, non-echoing error returned by the preparation API."""

    def __init__(self, status_code: int, code: str) -> None:
        self.status_code = status_code
        self.code = code
        super().__init__(code)
