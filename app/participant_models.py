"""Strict, free-text-free contracts for the formative participant store."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

NOTICE_VERSION = "finance-formative-notice-20261020-r2"
POLICY_VERSION = "finance-formative-policy-20261020-r2"


class ConsentInput(BaseModel):
    """Affirmative consent flags; versions are pinned by the service contract."""

    model_config = ConfigDict(extra="forbid", strict=True)

    adult_confirmed: StrictBool
    participation_agreed: StrictBool
    observation_recording_agreed: StrictBool
    notice_version: Literal["finance-formative-notice-20261020-r2"]
    policy_version: Literal["finance-formative-policy-20261020-r2"]


TaskStatus = Literal[
    "completed",
    "partial",
    "abandoned",
    "timeout",
    "safety_stopped",
    "technical_failure",
    "not_run",
]
ObservedChoice = Literal["yes", "no", "unclear", "not_observed"]
Comprehension = Literal["observed_understood", "observed_misunderstood", "not_observed", "unclear"]
CriticalMisunderstanding = Literal["yes", "no_observed", "not_observed", "unclear"]


class ObservationInput(BaseModel):
    """One coded observation snapshot with no free-text fields."""

    model_config = ConfigDict(extra="forbid", strict=True)

    task_status: TaskStatus
    elapsed_seconds_monotonic: StrictInt | None = Field(ge=0, le=360)
    help_requested_during_task: ObservedChoice
    content_help_during_task: ObservedChoice
    technical_issue_during_task: ObservedChoice
    stuck_event_count_during_task: StrictInt | None = Field(ge=0, le=100)
    next_action_comprehension_post_task: Comprehension
    source_link_date_location_comprehension_post_task: Comprehension
    preparation_list_meaning_comprehension_post_task: Comprehension
    card_limits_comprehension_post_task: Comprehension
    critical_misunderstanding_observed: CriticalMisunderstanding
    critical_misunderstanding_phase: Literal["during_task", "post_task", "both", "unknown"]
    sensitive_data_handling_status: Literal[
        "not_observed",
        "incident_pending",
        "restricted_pending_deletion",
        "deletion_verified",
        "unknown",
    ]
    recording_status: Literal["not_recorded"]

    @model_validator(mode="after")
    def validate_cross_fields(self) -> "ObservationInput":
        if self.task_status != "not_run" and (
            self.elapsed_seconds_monotonic is None or self.stuck_event_count_during_task is None
        ):
            raise ValueError("observed_task_requires_metrics")
        if self.task_status == "timeout" and self.elapsed_seconds_monotonic != 360:
            raise ValueError("timeout_requires_360_seconds")
        if self.task_status == "not_run":
            if (
                self.elapsed_seconds_monotonic is not None
                or self.stuck_event_count_during_task is not None
            ):
                raise ValueError("not_run_requires_null_metrics")
            task_choices = (
                self.help_requested_during_task,
                self.content_help_during_task,
                self.technical_issue_during_task,
            )
            comprehension = (
                self.next_action_comprehension_post_task,
                self.source_link_date_location_comprehension_post_task,
                self.preparation_list_meaning_comprehension_post_task,
                self.card_limits_comprehension_post_task,
            )
            if any(value != "not_observed" for value in task_choices + comprehension):
                raise ValueError("not_run_requires_not_observed")
            if (
                self.critical_misunderstanding_observed != "not_observed"
                or self.critical_misunderstanding_phase != "unknown"
            ):
                raise ValueError("not_run_requires_unknown_critical_phase")
        if self.critical_misunderstanding_observed == "yes":
            if self.critical_misunderstanding_phase == "unknown":
                raise ValueError("critical_misunderstanding_requires_phase")
        elif self.critical_misunderstanding_phase != "unknown":
            raise ValueError("phase_requires_critical_misunderstanding")
        return self
