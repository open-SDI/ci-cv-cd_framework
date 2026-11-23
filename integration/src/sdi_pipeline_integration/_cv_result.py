from __future__ import annotations

from typing import Annotated, Literal, Self, get_args

from pydantic import Field, NonNegativeFloat, NonNegativeInt, model_validator

from ._contracts import ContractModel, NonBlank

RequirementId = Literal[
    "secure-book",
    "accept-spoken-request",
    "resolve-rendezvous",
    "navigate-to-rendezvous",
    "verify-recipient-face",
    "transfer-book",
    "acknowledge-receipt",
    "return-to-origin",
]
REQUIREMENTS: tuple[RequirementId, ...] = get_args(RequirementId)
SCRIPTED: frozenset[RequirementId] = frozenset(
    {"secure-book", "resolve-rendezvous", "transfer-book"}
)
StepStatus = Literal["passed", "failed", "timed_out", "scripted", "not_reached"]
ENDED: frozenset[StepStatus] = frozenset({"passed", "failed", "timed_out"})


class Setup(ContractModel):
    status: Literal["passed", "timed_out"]
    elapsed_s: NonNegativeFloat
    waited_on: NonBlank | None = None

    @model_validator(mode="after")
    def name_the_wait_only_on_timeout(self) -> Self:
        if (self.waited_on is None) != (self.status == "passed"):
            msg = "setup names what it waited on exactly when it timed out"
            raise ValueError(msg)
        return self


class Step(ContractModel):
    requirement_id: RequirementId
    status: StepStatus
    waited_on: NonBlank | None = None
    elapsed_s: NonNegativeFloat | None = None

    @model_validator(mode="after")
    def match_the_requirement(self) -> Self:
        scripted = self.requirement_id in SCRIPTED
        if (self.status == "scripted") != scripted and self.status != "not_reached":
            msg = f"{self.requirement_id} cannot be {self.status}"
            raise ValueError(msg)
        if (self.waited_on is None) != scripted:
            msg = "a step waits on something exactly when it is not scripted"
            raise ValueError(msg)
        if (self.elapsed_s is None) == (self.status in ENDED):
            msg = "a step carries elapsed_s exactly when it passed, failed or timed out"
            raise ValueError(msg)
        return self


class Mission(ContractModel):
    setup: Setup
    steps: list[Step]

    @model_validator(mode="after")
    def follow_the_mission(self) -> Self:
        if tuple(item.requirement_id for item in self.steps) != REQUIREMENTS:
            msg = "the mission has the eight S-04 steps in requirement order"
            raise ValueError(msg)
        ended = self.setup.status == "timed_out"
        for item in self.steps:
            if ended != (item.status == "not_reached"):
                msg = "only the steps after a step that did not pass are not reached"
                raise ValueError(msg)
            ended = ended or item.status in {"failed", "timed_out"}
        return self

    @property
    def completed(self) -> bool:
        return self.setup.status == "passed" and all(
            item.status in {"passed", "scripted"} for item in self.steps
        )


class UnloadableType(ContractModel):
    topic: NonBlank
    type: NonBlank


class GraphTopic(ContractModel):
    topic: NonBlank
    types: list[NonBlank]
    publishers: list[NonBlank]


class ScenarioResult(ContractModel):
    unloadable_types: list[UnloadableType]
    counts: dict[NonBlank, NonNegativeInt]
    mission: Mission | None = None
    graph: Annotated[list[GraphTopic], Field(max_length=1024)]

    @model_validator(mode="after")
    def run_only_with_every_type(self) -> Self:
        if self.unloadable_types and (self.counts or self.mission is not None):
            msg = "a result with unloadable types has no counts and no mission"
            raise ValueError(msg)
        if not self.unloadable_types and self.mission is None:
            msg = "a result with every type loaded has a mission"
            raise ValueError(msg)
        return self
