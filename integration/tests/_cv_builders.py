from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from sdi_pipeline_integration._cv_result import GraphTopic, Mission, ScenarioResult
from sdi_pipeline_integration._cv_run import (
    Blocked,
    ContainerState,
    Errored,
    FailedToStart,
    Finished,
    Snapshot,
)
from sdi_pipeline_integration._cv_verdict import Composed
from sdi_pipeline_integration._domain_contracts import (
    BuiltImageRecord,
    FailedImageRecord,
    ValidationEvidence,
)
from sdi_pipeline_integration._result_contracts import (
    PipelineIntegrationResult,
    run_is_red,
)
from sdi_pipeline_integration._stage_contracts import (
    AcceptedAttemptEnvelope,
    Correlation,
    Reason,
)
from tests._cv_evidence import WAITED_ON, evidence

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from sdi_pipeline_integration._adapter_kit import Outcome, Succeeded
    from sdi_pipeline_integration._cv_run import (
        BlockingCause,
        CountingOutput,
        Phase,
        Placement,
        RunError,
        RunOutcome,
    )
    from sdi_pipeline_integration._cv_verdict import CvError, Judgement, Runner
    from sdi_pipeline_integration._domain_contracts import ImageRecord

POSE = "geometry_msgs/msg/PoseWithCovarianceStamped"
STRING = "std_msgs/msg/String"
TWIST = "geometry_msgs/msg/TwistStamped"
IMAGE = "sensor_msgs/msg/Image"
INDEX_DIGEST = f"sha256:{'1' * 64}"
ARM64_DIGEST = f"sha256:{'2' * 64}"
PHASES: dict[Phase, float] = {
    "leftover-cleanup": 0.4,
    "sim-build": 1.2,
    "pulls": 3.5,
    "start": 2.0,
    "orchestrator-wait": 60.1,
}
ImageKind = Literal["built", "arm64-only", "build-failed", "unresolved", "missing"]
type Requested = tuple[tuple[Placement, ...], tuple[CountingOutput, ...]]
type RunFor = Callable[[tuple[Placement, ...], tuple[CountingOutput, ...]], RunOutcome]


def amd64_digest(service_id: str) -> str:
    return f"sha256:{sum(map(ord, service_id)) % 10**6:064d}"


def image(service_id: str, kind: ImageKind = "built") -> ImageRecord | None:
    match kind:
        case "built" | "arm64-only":
            platforms = [{"architecture": "arm64", "digest": ARM64_DIGEST}]
            if kind == "built":
                platforms.insert(
                    0, {"architecture": "amd64", "digest": amd64_digest(service_id)}
                )
            return BuiltImageRecord.model_validate(
                {
                    "service_id": service_id,
                    "status": "built",
                    "repository": f"sdi/{service_id}",
                    "digest": INDEX_DIGEST,
                    "platforms": platforms,
                }
            )
        case "build-failed" | "unresolved":
            reason = (
                "build-failed" if kind == "build-failed" else "unresolved-placement"
            )
            return FailedImageRecord(
                service_id=service_id,
                status="failed",
                reason=reason,
                detail="recorded by a test",
            )
        case "missing":
            return None


def composed(
    service_id: str,
    publishes: Mapping[str, str] | None = None,
    *,
    devices: Sequence[str] = (),
    kind: ImageKind = "built",
) -> Composed:
    return Composed(
        service_id,
        image(service_id, kind),
        frozenset(devices),
        dict(publishes or {}),
    )


def running(*log: str) -> ContainerState:
    return ContainerState("running", 0, log or ("ready",))


def exited(code: int, *log: str) -> ContainerState:
    return ContainerState("exited", code, log or (f"exiting with {code}",))


def unstartable(detail: str = "no such image") -> FailedToStart:
    return FailedToStart(detail)


def mission(
    first_unmet: str | None = None,
    status: Literal["failed", "timed_out"] = "timed_out",
    *,
    setup: Literal["passed", "timed_out"] = "passed",
) -> Mission:
    reached = setup == "passed"
    steps: list[dict[str, Any]] = []
    for requirement_id, waited_on in WAITED_ON.items():
        step: dict[str, Any] = {"requirement_id": requirement_id}
        if waited_on is not None:
            step["waited_on"] = waited_on
        if not reached:
            step["status"] = "not_reached"
        elif requirement_id == first_unmet:
            step |= {"status": status, "elapsed_s": 60.0}
            reached = False
        elif waited_on is None:
            step["status"] = "scripted"
        else:
            step |= {"status": "passed", "elapsed_s": 2.5}
        steps.append(step)
    setup_record: dict[str, Any] = {"status": setup, "elapsed_s": 3.0}
    if setup == "timed_out":
        setup_record |= {"elapsed_s": 300.0, "waited_on": "/bt_navigator/get_state"}
    return Mission.model_validate({"setup": setup_record, "steps": steps})


def finished(  # noqa: PLR0913
    *,
    counts: Mapping[str, int] | None = None,
    states: Mapping[str, ContainerState | FailedToStart] | None = None,
    scenario_mission: Mission | None = None,
    unloadable: Mapping[str, str] | None = None,
    orchestrator: ContainerState | None = None,
    graph: Sequence[GraphTopic] = (),
) -> RunFor:

    def run(
        placements: tuple[Placement, ...], counting: tuple[CountingOutput, ...]
    ) -> Finished:
        if unloadable:
            result = ScenarioResult.model_validate(
                {
                    "unloadable_types": [
                        {"topic": topic, "type": kind}
                        for topic, kind in unloadable.items()
                    ],
                    "counts": {},
                    "graph": [],
                }
            )
        else:
            result = ScenarioResult.model_validate(
                {
                    "unloadable_types": [],
                    "counts": {item.topic: 5 for item in counting} | dict(counts or {}),
                    "mission": (scenario_mission or mission()).model_dump(
                        exclude_none=True
                    ),
                    "graph": [item.model_dump() for item in graph],
                }
            )
        snapshot = Snapshot(
            running(),
            orchestrator or exited(0),
            {
                item.service_id: (states or {}).get(item.service_id, running())
                for item in placements
            },
        )
        return Finished(result, snapshot, PHASES)

    return run


def runner(outcome: RunOutcome | RunFor, seen: list[Requested] | None = None) -> Runner:

    def run(
        placements: tuple[Placement, ...], counting: tuple[CountingOutput, ...]
    ) -> RunOutcome | CvError:
        if seen is not None:
            seen.append((placements, counting))
        if callable(outcome):
            return outcome(placements, counting)
        return outcome

    return run


def blocked(causes: Mapping[str, BlockingCause]) -> Blocked:
    return Blocked(causes, PHASES)


def errored(error: RunError, *, snapshot: bool = True) -> Errored:
    state = (
        Snapshot(exited(1, "gz sim crashed"), running("waiting for setup"), {})
        if snapshot
        else None
    )
    return Errored(error, f"{error} raised by a test", state, PHASES)


def never(
    placements: tuple[Placement, ...], counting: tuple[CountingOutput, ...]
) -> RunOutcome:
    raise AssertionError(("the runner ran", placements, counting))


def evidence_of(judgement: Judgement) -> ValidationEvidence:
    return ValidationEvidence.model_validate(
        evidence(
            judgement.outcome.domain_outcome,
            [
                item.model_dump(mode="json", exclude_none=True)
                for item in judgement.services
            ],
            None
            if judgement.mission is None
            else judgement.mission.model_dump(mode="json", exclude_none=True),
        )
    )


def run_is_red_for(outcome: Outcome | Succeeded) -> bool:
    reason = (
        None
        if outcome.code == "succeeded"
        else Reason.model_construct(
            category="domain", code=f"sdi.cv.{outcome.code}", summary="test"
        )
    )
    attempt = AcceptedAttemptEnvelope.model_construct(
        correlation=Correlation.model_construct(stage="cv"),
        execution_conclusion=outcome.conclusion,
        domain_outcome=outcome.domain_outcome,
        reason=reason,
    )
    return run_is_red(PipelineIntegrationResult.model_construct(attempts=[attempt]))
