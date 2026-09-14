"""Authoritative non-secret receipt for one GitHub-to-Jenkins handoff."""

from __future__ import annotations

from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BeforeValidator,
    Field,
    NonNegativeFloat,
    PositiveInt,
    StringConstraints,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema  # noqa: TC002

from ._contracts import ContractModel
from ._stage_contracts import ExecutionId, parse_wire_datetime

HANDOFF_RECEIPT_SCHEMA_VERSION = "sdi.github-jenkins-handoff-receipt/v1"
HANDOFF_CONTRACT_VERSION = "sdi.github-jenkins-handoff/v1"
HANDOFF_RECEIPT_PATH = "handoff-receipt.json"

type HandoffPhase = Literal[
    "validating",
    "preflighting",
    "submitting",
    "queued",
    "running",
    "retrieving",
    "completed",
    "failed",
    "cancelled",
]
type JenkinsResult = Literal["SUCCESS", "FAILURE", "ABORTED", "UNSTABLE", "NOT_BUILT"]
type FailureCode = Literal[
    "input_validation",
    "configuration",
    "reachability",
    "authentication",
    "authorization",
    "http_operation",
    "submission_deadline",
    "indeterminate_submission",
    "queue_deadline",
    "queue_cancelled",
    "queue_unavailable",
    "build_deadline",
    "retrieval_deadline",
    "outer_deadline",
    "jenkins_failure",
    "jenkins_aborted",
    "jenkins_unstable",
    "missing_artifact",
    "invalid_artifact",
    "identity_mismatch",
]
WireDatetime = Annotated[AwareDatetime, BeforeValidator(parse_wire_datetime)]

QueueRelativeUrl = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^queue/item/[1-9][0-9]*/$"),
]
BuildRelativeUrl = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^job/[A-Za-z0-9_.-]+(?:/job/[A-Za-z0-9_.-]+)*/[1-9][0-9]*/$",
    ),
]


class GitHubRunReference(ContractModel):
    """GitHub workflow provenance for this handoff."""

    run_id: PositiveInt
    run_attempt: PositiveInt


class HandoffTransition(ContractModel):
    """One accepted, sanitized handoff phase transition."""

    phase: HandoffPhase
    observed_at: WireDatetime


class JenkinsQueueReference(ContractModel):
    """The exact Jenkins queue item returned by submission."""

    queue_id: PositiveInt
    relative_url: QueueRelativeUrl

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        if self.relative_url != f"queue/item/{self.queue_id}/":
            msg = "queue relative URL does not match its queue ID"
            raise ValueError(msg)
        return self


class JenkinsBuildReference(ContractModel):
    """The exact executable assigned to the correlated queue item."""

    build_number: PositiveInt
    relative_url: BuildRelativeUrl

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        if not self.relative_url.endswith(f"/{self.build_number}/"):
            msg = "build relative URL does not match its build number"
            raise ValueError(msg)
        return self


class SuccessfulTerminal(ContractModel):
    """A handoff that returned one validated complete bundle."""

    outcome: Literal["succeeded"]
    observed_at: WireDatetime
    jenkins_result: Literal["SUCCESS"]


class FailedTerminal(ContractModel):
    """A sanitized typed handoff failure."""

    outcome: Literal["failed"]
    observed_at: WireDatetime
    code: FailureCode
    phase: HandoffPhase
    limit_seconds: PositiveInt | SkipJsonSchema[None] = None
    elapsed_seconds: NonNegativeFloat | SkipJsonSchema[None] = None
    last_known_state: HandoffPhase | SkipJsonSchema[None] = None
    jenkins_result: JenkinsResult | SkipJsonSchema[None] = None

    @model_validator(mode="after")
    def validate_deadline_facts(self) -> Self:
        deadline_codes = {
            "submission_deadline",
            "queue_deadline",
            "build_deadline",
            "retrieval_deadline",
            "outer_deadline",
        }
        if self.code in deadline_codes and (
            self.limit_seconds is None or self.elapsed_seconds is None
        ):
            msg = "deadline failures require their configured limit and elapsed time"
            raise ValueError(msg)
        return self


class CancelledTerminal(ContractModel):
    """A cancelled handoff with the bounded observation available at exit."""

    outcome: Literal["cancelled"]
    observed_at: WireDatetime
    jenkins_result: JenkinsResult | SkipJsonSchema[None] = None


type HandoffTerminal = Annotated[
    SuccessfulTerminal | FailedTerminal | CancelledTerminal,
    Field(discriminator="outcome"),
]


class HandoffReceipt(ContractModel):
    """Accumulating non-secret GitHub and exact Jenkins correlation facts."""

    schema_version: Literal["sdi.github-jenkins-handoff-receipt/v1"]
    execution_id: ExecutionId
    github: GitHubRunReference
    phase: HandoffPhase
    transitions: Annotated[list[HandoffTransition], Field(min_length=1)]
    queue: JenkinsQueueReference | SkipJsonSchema[None] = None
    build: JenkinsBuildReference | SkipJsonSchema[None] = None
    terminal: HandoffTerminal | SkipJsonSchema[None] = None

    @model_validator(mode="after")
    def validate_progress(self) -> Self:  # noqa: C901, PLR0912
        phases = [transition.phase for transition in self.transitions]
        if phases[0] != "validating" or phases[-1] != self.phase:
            msg = "receipt transitions must start at validating and end at its phase"
            raise ValueError(msg)
        if len(phases) != len(set(phases)):
            msg = "receipt phases must not repeat"
            raise ValueError(msg)
        if any(
            current.observed_at < previous.observed_at
            for previous, current in pairwise(self.transitions)
        ):
            msg = "receipt transition timestamps must be monotonic"
            raise ValueError(msg)
        allowed_next = {
            "validating": {"preflighting", "failed", "cancelled"},
            "preflighting": {"submitting", "failed", "cancelled"},
            "submitting": {"queued", "failed", "cancelled"},
            "queued": {"running", "failed", "cancelled"},
            "running": {"retrieving", "failed", "cancelled"},
            "retrieving": {"completed", "failed", "cancelled"},
        }
        if any(
            current not in allowed_next or following not in allowed_next[current]
            for current, following in pairwise(phases)
        ):
            msg = "receipt phases must follow the handoff lifecycle"
            raise ValueError(msg)
        terminal_phases = {"completed", "failed", "cancelled"}
        if any(phase in terminal_phases for phase in phases[:-1]):
            msg = "a terminal receipt phase must be the final transition"
            raise ValueError(msg)
        if (self.terminal is None) != (self.phase not in terminal_phases):
            msg = "terminal facts must be present exactly for a terminal phase"
            raise ValueError(msg)
        if self.build is not None and self.queue is None:
            msg = "a Jenkins build cannot be recorded without its queue item"
            raise ValueError(msg)
        reached_queue = "queued" in phases
        reached_build = "running" in phases
        if (self.queue is None) == reached_queue:
            msg = "queue facts must be present exactly after queue correlation"
            raise ValueError(msg)
        if (self.build is None) == reached_build:
            msg = "build facts must be present exactly after build correlation"
            raise ValueError(msg)
        if isinstance(self.terminal, SuccessfulTerminal) and (
            self.phase != "completed" or self.build is None
        ):
            msg = "successful terminal facts require a completed correlated build"
            raise ValueError(msg)
        if isinstance(self.terminal, FailedTerminal):
            if self.phase != "failed":
                msg = "failed terminal facts require the failed phase"
                raise ValueError(msg)
            interrupted_phase = phases[-2] if len(phases) > 1 else None
            if self.terminal.phase != interrupted_phase:
                msg = "failed terminal facts must identify the interrupted phase"
                raise ValueError(msg)
        if isinstance(self.terminal, CancelledTerminal) and self.phase != "cancelled":
            msg = "cancelled terminal facts require the cancelled phase"
            raise ValueError(msg)
        return self
