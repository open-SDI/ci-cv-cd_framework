from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, get_args

from pydantic import TypeAdapter

from ._adapter_kit import SUCCEEDED, Outcome, Succeeded
from ._cv_run import (
    Blocked,
    ContainerState,
    CountingOutput,
    Errored,
    Finished,
    Placement,
    RunError,
)
from ._domain_contracts import (
    UNEVALUATED_REASONS,
    BuiltImageRecord,
    FailedImageRecord,
    Mission,
    NonParticipatingService,
    ServiceParticipation,
    UnevaluatedService,
    validation_verdict,
)
from ._image_build_redaction import failure_text

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from ._cv_result import GraphTopic
    from ._cv_result import Mission as ScenarioMission
    from ._cv_run import RunOutcome
    from ._domain_contracts import ImageRecord, UnevaluatedReason
    from ._stage_contracts import ReasonCategory

TOPIC_CONSTANT = frozenset(
    {
        "/clock",
        "/odom",
        "/tf",
        "/tf_static",
        "/scan",
        "/image",
        "/camera_info",
        "/joint_states",
        "/imu",
        "/robot_description",
        "/initialpose",
        "/audio_listener/audio",
        "/face_enrollment",
        "/rosout",
        "/parameter_events",
    }
)
MACHINERY_NODES = frozenset(
    {"/s04_bridge", "/robot_state_publisher", "/cv_orchestrator"}
)
SIMULATED_DEVICES = frozenset({"opencr", "lidar", "camera", "microphone"})

CvOnlyError = Literal[
    "engine-unreachable", "input-mismatch", "unsupported-robot", "evidence-too-large"
]
CvErrorCode = RunError | CvOnlyError
DeclarationCause = Literal[
    "image-unavailable", "no-amd64", "needs-gpu-or-device", "no-counting-output"
]
BLOCKING_DETAILS: dict[UnevaluatedReason, str] = {
    "image-unavailable": "the CV engine could not pull the image",
    "unsupported-command": "the image command is neither ros2 launch nor ros2 run",
}
SERVICE_PARTICIPATION: TypeAdapter[ServiceParticipation] = TypeAdapter(
    ServiceParticipation
)


@dataclass(frozen=True)
class Composed:
    service_id: str
    image: ImageRecord | None
    devices: frozenset[str]
    publishes: Mapping[str, str]


@dataclass(frozen=True)
class CvError:
    code: CvErrorCode
    detail: str
    run: Errored | None = None

    @property
    def outcome(self) -> Outcome:
        return ERRORS[self.code]


@dataclass(frozen=True)
class Judgement:
    services: tuple[ServiceParticipation, ...]
    mission: Mission | None
    counting: tuple[CountingOutput, ...]
    run: Blocked | Finished | None

    @property
    def outcome(self) -> Outcome | Succeeded:
        match validation_verdict(self.services, self.mission):
            case "succeeded":
                return SUCCEEDED
            case "failed":
                return Outcome(
                    "failed", "succeeded", "failed", "domain", self._failure()
                )
            case "not_evaluated":
                return Outcome(
                    "not-evaluated",
                    "succeeded",
                    "not_evaluated",
                    "dependency",
                    self._gap(),
                )

    def _failure(self) -> str:
        for item in self.services:
            if isinstance(item, NonParticipatingService):
                exit_code = (
                    "" if item.exit_code is None else f" (exit {item.exit_code})"
                )
                return (
                    f"{item.service_id} did not participate: {item.reason}{exit_code}."
                )
        steps = [] if self.mission is None else self.mission.steps
        step = next(
            (item for item in steps if item.status in {"failed", "timed_out"}), None
        )
        if step is None:
            return "The mission setup timed out."
        status = step.status.replace("_", " ")
        return f"Mission step {step.requirement_id} {status} on {step.waited_on}."

    def _gap(self) -> str:
        unevaluated = [
            item for item in self.services if isinstance(item, UnevaluatedService)
        ]
        first = min(
            unevaluated, key=lambda item: UNEVALUATED_REASONS.index(item.reason)
        )
        return f"{first.service_id} was not evaluated: {first.reason}."


type Runner = Callable[
    [tuple[Placement, ...], tuple[CountingOutput, ...]], RunOutcome | CvError
]


@dataclass(frozen=True)
class _Strict:
    service_id: str
    digest: str
    counting: tuple[CountingOutput, ...]


@dataclass(frozen=True)
class _Loosened:
    service_id: str
    digest: str

    @property
    def counting(self) -> tuple[CountingOutput, ...]:
        return ()


type _Cleared = _Strict | _Loosened


@dataclass(frozen=True)
class _Unfit:
    service_id: str
    cause: DeclarationCause
    detail: str


def assess(services: Sequence[Composed], run: Runner) -> Judgement | CvError:
    declared = [_declare(item, services) for item in services]
    cleared = [item for item in declared if not isinstance(item, _Unfit)]
    if len(cleared) < len(declared):
        return Judgement(
            tuple(
                _unevaluated(item.service_id, item.cause, item.detail)
                if isinstance(item, _Unfit)
                else _unevaluated(item.service_id, "blocked")
                for item in declared
            ),
            None,
            (),
            None,
        )
    counting = tuple(output for item in cleared for output in item.counting)
    outcome = run(
        tuple(Placement(item.service_id, item.digest) for item in cleared), counting
    )
    match outcome:
        case CvError():
            return outcome
        case Errored():
            return CvError(outcome.error, outcome.detail, outcome)
        case Blocked():
            reasons: list[UnevaluatedReason] = [
                outcome.causes.get(item.service_id, "blocked") for item in cleared
            ]
            return Judgement(
                tuple(
                    _unevaluated(item.service_id, reason, BLOCKING_DETAILS.get(reason))
                    for item, reason in zip(cleared, reasons, strict=True)
                ),
                None,
                counting,
                outcome,
            )
        case Finished():
            return _finished(cleared, counting, outcome)


def topic_constant_drift(
    graph: Sequence[GraphTopic],
) -> tuple[frozenset[str], frozenset[str]]:
    published = frozenset(
        item.topic for item in graph if MACHINERY_NODES & set(item.publishers)
    )
    return TOPIC_CONSTANT - published, published - TOPIC_CONSTANT


def _declare(service: Composed, services: Sequence[Composed]) -> _Cleared | _Unfit:
    image = service.image
    if not isinstance(image, BuiltImageRecord):
        return _Unfit(service.service_id, "image-unavailable", _unbuilt(image))
    digest = next(
        (item.digest for item in image.platforms if item.architecture == "amd64"),
        None,
    )
    if digest is None:
        return _Unfit(
            service.service_id, "no-amd64", "image-build pushed no amd64 image"
        )
    unsimulated = sorted(service.devices - SIMULATED_DEVICES)
    if unsimulated:
        return _Unfit(
            service.service_id,
            "needs-gpu-or-device",
            f"declares {' '.join(unsimulated)}",
        )
    if service.devices:
        return _Loosened(service.service_id, digest)
    others = {
        topic: other.service_id
        for other in services
        if other.service_id != service.service_id
        for topic in other.publishes
    }
    own = sorted(topic for topic in service.publishes if topic not in TOPIC_CONSTANT)
    counting = tuple(
        CountingOutput(
            topic=topic, type=service.publishes[topic], service_id=service.service_id
        )
        for topic in own
        if topic not in others
    )
    if counting:
        return _Strict(service.service_id, digest, counting)
    if not service.publishes:
        detail = "declares no published topic"
    elif not own:
        detail = "all outputs in the topic constant"
    else:
        sharers = sorted({others[topic] for topic in own})
        detail = f"all outputs shared with {' '.join(sharers)}"
    return _Unfit(service.service_id, "no-counting-output", detail)


def _unbuilt(image: ImageRecord | None) -> str:
    if image is None:
        return "image-build recorded no image"
    if isinstance(image, FailedImageRecord):
        return f"image-build recorded {image.reason}"
    return "image-build did not evaluate the image"


def _unevaluated(
    service_id: str,
    reason: UnevaluatedReason,
    detail: str | None = None,
    image_digest: str | None = None,
) -> ServiceParticipation:
    return _service(
        service_id,
        "not_evaluated",
        reason=reason,
        detail=None if detail is None else failure_text(detail, fallback=reason),
        image_digest=image_digest,
    )


def _service(
    service_id: str, participation: str, **facts: object
) -> ServiceParticipation:
    return SERVICE_PARTICIPATION.validate_python(
        {"service_id": service_id, "participation": participation}
        | {name: value for name, value in facts.items() if value is not None}
    )


def _finished(
    cleared: Sequence[_Cleared],
    counting: tuple[CountingOutput, ...],
    finished: Finished,
) -> Judgement | CvError:
    mission = finished.result.mission
    if mission is not None:
        return Judgement(
            tuple(_participation(item, finished) for item in cleared),
            _mission(mission),
            counting,
            finished,
        )
    owners = {item.topic: item.service_id for item in counting}
    types: dict[str, list[str]] = {}
    for item in finished.result.unloadable_types:
        if item.topic in owners:
            types.setdefault(owners[item.topic], []).append(item.type)
    if not types:
        detail = "the result names unloadable types on no counting topic"
        return CvError(
            "invalid-result",
            detail,
            Errored("invalid-result", detail, finished.snapshot, finished.phases),
        )
    placements = finished.snapshot.placements
    return Judgement(
        tuple(
            _unevaluated(
                item.service_id,
                "unloadable-type" if item.service_id in types else "blocked",
                " ".join(types[item.service_id]) if item.service_id in types else None,
                item.digest
                if isinstance(placements.get(item.service_id), ContainerState)
                else None,
            )
            for item in cleared
        ),
        None,
        counting,
        finished,
    )


def _participation(cleared: _Cleared, finished: Finished) -> ServiceParticipation:
    state = finished.snapshot.placements[cleared.service_id]
    if not isinstance(state, ContainerState):
        return _service(
            cleared.service_id,
            "did_not_participate",
            reason="failed-to-start",
            detail=failure_text(state.detail, fallback="the container did not start"),
        )
    counts = {
        item.topic: finished.result.counts[item.topic] for item in cleared.counting
    }
    started = {
        "image_digest": cleared.digest,
        "counting_topics": [
            {"topic": topic, "messages": messages} for topic, messages in counts.items()
        ],
    }
    if not state.running:
        return _service(
            cleared.service_id,
            "did_not_participate",
            reason="container-exited",
            exit_code=state.exit_code,
            **started,
        )
    if isinstance(cleared, _Loosened):
        return _service(cleared.service_id, "participated_loosened", **started)
    if any(messages > 0 for messages in counts.values()):
        return _service(cleared.service_id, "participated", **started)
    return _service(
        cleared.service_id, "did_not_participate", reason="no-message", **started
    )


def _mission(mission: ScenarioMission) -> Mission:
    return Mission.model_validate(
        {
            "setup": mission.setup.status,
            "steps": [
                {"requirement_id": item.requirement_id, "status": item.status}
                | ({} if item.waited_on is None else {"waited_on": item.waited_on})
                for item in mission.steps
            ],
        }
    )


_ERRORS: dict[CvErrorCode, tuple[ReasonCategory, str]] = {
    "engine-unreachable": ("dependency", "The CV engine did not answer."),
    "pull-failed": ("dependency", "The CV engine lost the registry while pulling."),
    "sim-build-failed": ("tool", "The sim image did not build."),
    "start-failed": ("tool", "The sim or the orchestrator did not start."),
    "no-time-to-start": ("tool", "The work limit ran out before the scenario started."),
    "orchestrator-timeout": (
        "tool",
        "The orchestrator did not finish within the work limit.",
    ),
    "sim-not-running": ("tool", "The sim stopped before the orchestrator finished."),
    "no-result": ("tool", "The orchestrator wrote no result."),
    "result-too-large": ("tool", "The orchestrator result is over 64 KiB."),
    "invalid-result": ("tool", "The orchestrator result is not valid."),
    "evidence-too-large": ("tool", "The Validation evidence is over its size grant."),
    "input-mismatch": (
        "input",
        "The service repository differs from the one composition read.",
    ),
    "unsupported-robot": (
        "input",
        "CV simulates only TurtleBot3 Waffle and Burger targets.",
    ),
}
ERRORS: dict[CvErrorCode, Outcome] = {
    code: Outcome(code, "failed", "not_evaluated", *_ERRORS[code])
    for code in (*get_args(RunError), *get_args(CvOnlyError))
}
