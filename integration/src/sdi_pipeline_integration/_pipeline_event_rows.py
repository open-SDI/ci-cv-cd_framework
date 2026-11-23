"""Pipeline events: build one row from a run's identity, profile and attempts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Any,
    Final,
    Literal,
    TypedDict,
    assert_never,
)

from ._contracts import TargetExecutionProfile
from ._cv_result import REQUIREMENTS
from ._domain_contracts import (
    BuiltImageRecord,
    DeploymentResult,
    DeploymentSchema,
    FailedImageRecord,
    FailedPlacement,
    ImageBuildResult,
    NonParticipatingService,
    UndeployedPlacement,
    UnevaluatedService,
    ValidationEvidence,
    image_tag,
)
from ._json_input import parse_json
from ._pipeline_event_catalogue import SLOT_TYPES, STEP_BY_CODE, template_slots
from ._pipeline_events import (
    SCHEMA_VERSION,
    CvServiceIssue,
    ImageFailure,
    MissionStepEntry,
    PipelineEventRow,
    PlacementIssue,
    derive_ci_status,
    format_emitted_at,
    render_heartbeat,
    render_message,
)
from ._result_contracts import RED_REASON_CODES, archive_path
from ._stage_registry import STAGE_ORDER, STAGES, StageName
from ._yaml_input import InputError, parse_yaml, validate_contract

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from pydantic import BaseModel

    from ._domain_contracts import ServiceParticipation
    from ._pipeline_event_catalogue import (
        PipelineStatus,
        SlotName,
        SlotType,
        SlotValue,
        StepCode,
    )
    from ._pipeline_events import EventScope
    from ._stage_contracts import AcceptedAttemptEnvelope
    from ._stage_runtime import StageExecution, StageInputSource

type Architecture = Literal["amd64", "arm64"]
type Target = Literal["SDV", "SDR"]

# The row fills these slots from itself, so a message cannot contradict its row.
ROW_SLOTS: Final[frozenset[SlotName]] = frozenset(
    (
        "composition_id",
        "profile_id",
        "ci_status",
        "cv_status",
        "cd_status",
        "status",
        "reason",
    )
)
RUN_DEADLINE_EXCEEDED: Final = "sdi.run.deadline-exceeded"
PREREQUISITE_BLOCKED: Final = "sdi.dependency.prerequisite-blocked"


@dataclass(frozen=True)
class EventLine:
    """A line head other than `stage-finished`: Stage, step, and the caller's slots.

    It is also an accepted adapter report. Building one checks it against the
    catalogue and raises ValueError when the step does not appear on the
    Stage's lines, when `slots` names a slot the row fills, when no form (or
    the heartbeat template) takes exactly `slots`, or when a value does not fit
    its slot type.
    """

    stage: StageName | None
    step: StepCode
    slots: Mapping[SlotName, SlotValue] = field(
        default_factory=lambda: MappingProxyType({})
    )
    heartbeat: bool = False

    def __post_init__(self) -> None:
        problem = _line_problem(self)
        if problem is not None:
            msg = f"{self.stage or 'run-level'} {self.step} line: {problem}"
            raise ValueError(msg)
        object.__setattr__(self, "slots", MappingProxyType(dict(self.slots)))


@dataclass(frozen=True)
class StageFinished:
    """The `stage-finished` head, carrying the attempt its Stage just published."""

    execution: StageExecution

    @property
    def stage(self) -> StageName:
        """Return the Stage the attempt belongs to."""
        return self.execution.envelope.correlation.stage

    @property
    def step(self) -> StepCode:
        """Return the head's step code."""
        return "stage-finished"


type LineHead = EventLine | StageFinished


def _line_problem(line: EventLine) -> str | None:
    entry = STEP_BY_CODE.get(line.step)
    if entry is None:
        return "not a catalogue step"
    if line.step == "stage-finished":
        return "a stage-finished head is a StageFinished"
    if line.stage is not None and line.stage not in STAGES:
        return "not a Stage"
    if not entry.allows_stage(line.stage):
        return "the step does not appear on these lines"
    templates = (entry.heartbeat,) if line.heartbeat else entry.forms
    if not any(
        template is not None
        and template_slots(template) - {"Stage"} - ROW_SLOTS == frozenset(line.slots)
        for template in templates
    ):
        return f"no form takes slots {sorted(line.slots)}"
    misfits = sorted(
        name for name, value in line.slots.items() if not _fits(SLOT_TYPES[name], value)
    )
    return f"slots {misfits} do not fit their types" if misfits else None


def _fits(slot: SlotType, value: object) -> bool:
    match slot.kind:
        case "integer":
            return (
                isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value < 10**slot.max_length
            )
        case "enum" | "identifier":
            return (
                isinstance(value, str)
                and len(value) <= slot.max_length
                and re.fullmatch(slot.value_regex, value) is not None
            )


def build_row(  # noqa: PLR0913
    identified: Mapping[str, Any],
    target_profile_source: StageInputSource,
    executions: Sequence[StageExecution],
    *,
    line: LineHead,
    live_items: Sequence[EventLine],
    scope: EventScope,
    emitted_at: datetime,
) -> PipelineEventRow:
    """Build the full row one pipeline event prints.

    `identified` is what `identify_committed_run` returned. `executions` are the
    accepted attempts the caller holds, matched to Stages by
    `correlation.stage`; the last one for a Stage wins, and a `StageFinished`
    head's attempt replaces any for its Stage. The running Stage is the line's Stage on
    every Stage-level line except `stage-finished`. `live_items` are the
    adapter reports the runtime accepted, in arrival order; only those of the
    running Stage count, and only while that Stage has no attempt. It never
    raises for any profile bytes, attempts or result files.
    """
    attempts: dict[StageName, StageExecution] = {
        item.envelope.correlation.stage: item for item in executions
    }
    running: StageName | None = None
    match line:
        case StageFinished(execution=execution):
            attempts[line.stage] = execution
        case EventLine(stage=stage):
            running = stage
    profile = read_profile(target_profile_source)
    target, architecture = profile_identity(profile)
    states = stage_states(attempts, running=running)
    statuses = _status_fields(states, scope=scope)
    live = (
        fold_live(running, live_items)
        if running is not None and running not in attempts
        else LiveValues()
    )
    documents = StageDocuments.read(attempts)
    return PipelineEventRow(
        schema_version=SCHEMA_VERSION,
        emitted_at=format_emitted_at(emitted_at),
        execution_id=identified["execution_id"],
        final=line.step == "run-finished",
        stage=line.stage,
        step=line.step,
        message=_message(line, identified, states, statuses),
        composition_id=identified["combination_id"],
        scenario_id=identified["scenario_id"],
        testcase_id=identified["testcase_id"],
        profile_id=identified["profile_id"],
        target=target,
        architecture=architecture,
        **statuses,
        **_ci_fields(
            attempts,
            documents,
            profile,
            architecture,
            identified["execution_id"],
            ci_status=statuses["ci_status"],
        ),
        **_cv_fields(attempts.get("cv"), documents.validation, live),
        **_cd_fields(attempts.get("cd"), documents.deployment, live),
    )


def read_profile(source: StageInputSource) -> TargetExecutionProfile | None:
    """Parse the committed target profile, or return None when it is not one."""
    if source.media_type != "application/yaml":
        return None
    try:
        return validate_contract(
            TargetExecutionProfile,
            parse_yaml(source.content, source.source_path),
            source.source_path,
        )
    except InputError:
        return None


def profile_identity(
    profile: TargetExecutionProfile | None,
) -> tuple[Target | None, Architecture | None]:
    """Return `target` and `architecture`, both null unless both are known."""
    architecture = None if profile is None else row_architecture(profile)
    if profile is None or architecture is None:
        return None, None
    return ("SDV" if profile.target.kind == "sdv" else "SDR"), architecture


def row_architecture(profile: TargetExecutionProfile) -> Architecture | None:
    """Return the one architecture all hosts share, else the one besides native's.

    Without exactly one native host, or with more than one other architecture,
    the architecture is ambiguous and this returns None.
    """
    hosts = profile.hosts.values()
    architectures: set[Architecture] = {host.architecture for host in hosts}
    if len(architectures) == 1:
        return next(iter(architectures))
    natives = [host.architecture for host in hosts if host.role == "native"]
    others = architectures - set(natives)
    return next(iter(others)) if len(natives) == 1 and len(others) == 1 else None


@dataclass(frozen=True)
class StageState:
    """One Stage on one line: its status and its own attempt's Reason code."""

    status: PipelineStatus
    reason: str | None = None

    @property
    def tier(self) -> int | None:
        """Return the spec 5.3 tier, or None when this Stage gives no reason."""
        if self.reason is None or self.reason == PREREQUISITE_BLOCKED:
            return None
        match self.status:
            case "FAILED":
                return 1
            case "SKIPPED":
                return 2 if self.reason in RED_REASON_CODES else 3
            case "NOT_EVALUATED":
                return 3
            case "PENDING" | "RUNNING" | "SUCCEEDED":
                return None


def attempt_status(envelope: AcceptedAttemptEnvelope) -> PipelineStatus:
    """Return the status of one accepted attempt (spec 5.1)."""
    conclusion = envelope.execution_conclusion
    outcome = envelope.domain_outcome
    match conclusion:
        case "skipped":
            reason = envelope.reason
            deadline = reason is not None and reason.code == RUN_DEADLINE_EXCEEDED
            return "FAILED" if deadline else "SKIPPED"
        case "failed" | "timed_out":
            return "FAILED"
        case "succeeded":
            match outcome:
                case "succeeded":
                    return "SUCCEEDED"
                case "failed":
                    return "FAILED"
                case "not_evaluated":
                    return "NOT_EVALUATED"
                case _:
                    assert_never(outcome)
        case _:
            assert_never(conclusion)


def stage_states(
    attempts: Mapping[StageName, StageExecution], *, running: StageName | None
) -> dict[StageName, StageState]:
    """Return every Stage's state, with later Stages skipped after a blocker."""
    states: dict[StageName, StageState] = {}
    blocked = False
    for stage in STAGE_ORDER:
        execution = attempts.get(stage)
        if stage == running:
            states[stage] = StageState("RUNNING")
        elif execution is not None:
            envelope = execution.envelope
            code = None if envelope.reason is None else envelope.reason.code
            states[stage] = StageState(attempt_status(envelope), code)
            blocked = blocked or execution.blocks_downstream
        else:
            states[stage] = StageState("SKIPPED" if blocked else "PENDING")
    return states


def failure_reason(states: Mapping[StageName, StageState]) -> str | None:
    """Return the Reason of the earliest Stage in the highest tier (spec 5.3)."""
    ranked = [
        (state.tier, index, state.reason)
        for index, state in enumerate(states[stage] for stage in STAGE_ORDER)
        if state.tier is not None
    ]
    return min(ranked)[2] if ranked else None


class StatusFields(TypedDict):
    composition_status: PipelineStatus
    image_build_status: PipelineStatus
    ci_status: PipelineStatus
    cv_status: PipelineStatus
    cd_status: PipelineStatus
    failure_reason: str | None


def _status_fields(
    states: Mapping[StageName, StageState], *, scope: EventScope
) -> StatusFields:
    composition = states["composition"].status
    image_build = states["image_build"].status
    return StatusFields(
        composition_status=composition,
        image_build_status=image_build,
        ci_status=derive_ci_status(composition, image_build, scope=scope),
        cv_status=states["cv"].status,
        cd_status=states["cd"].status,
        failure_reason=failure_reason(states),
    )


def _message(
    line: LineHead,
    identified: Mapping[str, Any],
    states: Mapping[StageName, StageState],
    statuses: StatusFields,
) -> str:
    slots: dict[SlotName, SlotValue]
    match line:
        case StageFinished():
            state = states[line.stage]
            slots = {"status": state.status}
            if state.reason is not None:
                slots["reason"] = state.reason
            return render_message("stage-finished", stage=line.stage, slots=slots)
        case EventLine(step="run-accepted"):
            slots = {
                "composition_id": identified["combination_id"],
                "profile_id": identified["profile_id"],
            }
        case EventLine(step="run-finished"):
            slots = {
                "ci_status": statuses["ci_status"],
                "cv_status": statuses["cv_status"],
                "cd_status": statuses["cd_status"],
            }
        case EventLine(stage=stage, heartbeat=True) if stage is not None:
            return render_heartbeat(line.step, stage=stage, slots=line.slots)
        case EventLine():
            slots = dict(line.slots)
    return render_message(line.step, stage=line.stage, slots=slots)


@dataclass(frozen=True)
class StageDocuments:
    """The accepted result documents a row reads, each None when absent or broken."""

    deployment_schema: DeploymentSchema | None
    image_build: ImageBuildResult | None
    validation: ValidationEvidence | None
    deployment: DeploymentResult | None

    @classmethod
    def read(cls, attempts: Mapping[StageName, StageExecution]) -> StageDocuments:
        """Parse each document from its attempt's accepted files."""
        return cls(
            deployment_schema=_document(
                attempts.get("composition"), "deployment_schema", DeploymentSchema
            ),
            image_build=_document(
                attempts.get("image_build"), "image_build_result", ImageBuildResult
            ),
            validation=_document(
                attempts.get("cv"), "validation_evidence", ValidationEvidence
            ),
            deployment=_document(
                attempts.get("cd"), "deployment_result", DeploymentResult
            ),
        )


def _accepted_path(execution: StageExecution | None, slot: str) -> str | None:
    if execution is None:
        return None
    return next(
        (item.path for item in execution.envelope.accepted_files if item.slot == slot),
        None,
    )


def _document[ModelT: BaseModel](
    execution: StageExecution | None, slot: str, model: type[ModelT]
) -> ModelT | None:
    path = _accepted_path(execution, slot)
    content = None if execution is None or path is None else execution.files.get(path)
    if path is None or content is None:
        return None
    try:
        document = parse_json(content, path, max_bytes=max(1, len(content)))
        return validate_contract(model, document, path)
    except InputError:
        return None


def _ref(execution: StageExecution | None, stage: StageName, slot: str) -> str | None:
    path = _accepted_path(execution, slot)
    return None if path is None else archive_path(stage, path)


def _duration(execution: StageExecution | None) -> int | None:
    process = None if execution is None else execution.envelope.process
    return None if process is None else process.duration_ms


class CiFields(TypedDict):
    ci_duration_ms: int | None
    blueprint_ref: str | None
    deployment_schema_ref: str | None
    image_build_result_ref: str | None
    image_ref: list[str] | None
    image_digest: list[str] | None
    image_archive: None
    images_built: int | None
    images_total: int | None
    image_failures: list[ImageFailure] | None
    platform_status: Literal["MATCH", "MISMATCH"] | None
    detected_platform: str | None


def _ci_fields(  # noqa: PLR0913
    attempts: Mapping[StageName, StageExecution],
    documents: StageDocuments,
    profile: TargetExecutionProfile | None,
    architecture: Architecture | None,
    execution_id: str,
    *,
    ci_status: PipelineStatus,
) -> CiFields:
    composition = attempts.get("composition")
    image_build = attempts.get("image_build")
    durations = [
        duration
        for duration in (_duration(composition), _duration(image_build))
        if duration is not None
    ]
    ci_ended = ci_status not in {"PENDING", "RUNNING"}
    result = documents.image_build
    images = (
        NO_IMAGE_FIELDS
        if result is None
        else _image_fields(
            result, documents.deployment_schema, profile, architecture, execution_id
        )
    )
    return CiFields(
        ci_duration_ms=sum(durations) if ci_ended and durations else None,
        blueprint_ref=_ref(composition, "composition", "composition_blueprint"),
        deployment_schema_ref=_ref(composition, "composition", "deployment_schema"),
        image_build_result_ref=_ref(image_build, "image_build", "image_build_result"),
        image_archive=None,
        **images,
    )


class ImageFields(TypedDict):
    image_ref: list[str] | None
    image_digest: list[str] | None
    images_built: int | None
    images_total: int | None
    image_failures: list[ImageFailure] | None
    platform_status: Literal["MATCH", "MISMATCH"] | None
    detected_platform: str | None


NO_IMAGE_FIELDS: Final = ImageFields(
    image_ref=None,
    image_digest=None,
    images_built=None,
    images_total=None,
    image_failures=None,
    platform_status=None,
    detected_platform=None,
)


def _image_fields(
    result: ImageBuildResult,
    schema: DeploymentSchema | None,
    profile: TargetExecutionProfile | None,
    architecture: Architecture | None,
    execution_id: str,
) -> ImageFields:
    built = sorted(
        (item for item in result.images if isinstance(item, BuiltImageRecord)),
        key=lambda item: item.service_id,
    )
    placements = {} if schema is None else {p.service_id: p for p in schema.placements}
    artifacts = [
        placements[item.service_id].artifact_id
        if item.service_id in placements
        else None
        for item in built
    ]
    hosts = profile.hosts if profile is not None else {}
    chips: list[Architecture | None] = []
    for item in built:
        placement = placements.get(item.service_id)
        host = None if placement is None else hosts.get(placement.host or "")
        chips.append(None if host is None else host.architecture)
    platforms = [{image.architecture for image in item.platforms} for item in built]
    on_row_chip = [
        built_for
        for built_for, chip in zip(platforms, chips, strict=True)
        if architecture is not None and chip == architecture
    ]
    return ImageFields(
        image_ref=(
            None
            if None in artifacts
            else [
                image_tag(
                    service_id=item.service_id,
                    artifact_id=artifact,
                    execution_id=execution_id,
                )
                for item, artifact in zip(built, artifacts, strict=True)
                if artifact is not None
            ]
        ),
        image_digest=[item.digest for item in built],
        images_built=len(built),
        images_total=len(result.images),
        image_failures=[
            ImageFailure(service_id=item.service_id, reason=item.reason)
            for item in result.images
            if isinstance(item, FailedImageRecord)
        ],
        platform_status=(
            None
            if not built or None in chips
            else "MATCH"
            if all(
                chip in built_for
                for built_for, chip in zip(platforms, chips, strict=True)
            )
            else "MISMATCH"
        ),
        detected_platform=(
            f"linux/{architecture}"
            if on_row_chip
            and all(architecture in built_for for built_for in on_row_chip)
            else None
        ),
    )


@dataclass(frozen=True)
class LiveValues:
    """Item values the running Stage has reported; None means not reported yet."""

    cv_service_issues: tuple[CvServiceIssue, ...] | None = None
    mission_setup: str | None = None
    mission_steps: tuple[MissionStepEntry, ...] | None = None
    placement_issues: tuple[PlacementIssue, ...] | None = None


def fold_live(stage: StageName, reports: Sequence[EventLine]) -> LiveValues:
    """Fold the Stage's accepted reports into item values, first report wins."""
    services: dict[str, CvServiceIssue] = {}
    setup: str | None = None
    steps: dict[str, str] | None = None
    placements: dict[str, PlacementIssue] = {}
    for report in reports:
        if report.stage != stage or report.heartbeat:
            continue
        slots = {name: str(value) for name, value in report.slots.items()}
        match report.step:
            case "service-unfit" | "pull-blocked":
                reason = slots.get("unfit_reason") or slots["pull_reason"]
                services.setdefault(
                    slots["service_id"],
                    CvServiceIssue(
                        service_id=slots["service_id"],
                        participation="not_evaluated",
                        reason=reason,
                    ),
                )
            case "mission-setup-done":
                setup = setup or slots["setup_status"]
                steps = {} if steps is None else steps
            case "mission-step":
                steps = {} if steps is None else steps
                steps.setdefault(
                    slots["mission_step"], slots.get("mission_status", "scripted")
                )
            case "pod-not-running":
                placements.setdefault(
                    slots["service_id"],
                    PlacementIssue(
                        service_id=slots["service_id"],
                        host=slots["host"],
                        status="failed",
                        reason=slots["k8s_reason"],
                    ),
                )
            case _:
                pass
    return LiveValues(
        cv_service_issues=tuple(services.values()) or None,
        mission_setup=setup,
        mission_steps=None
        if steps is None
        else tuple(
            MissionStepEntry(id=step, status=steps.get(step)) for step in REQUIREMENTS
        ),
        placement_issues=tuple(placements.values()) or None,
    )


def _listed[ItemT](items: tuple[ItemT, ...] | None) -> list[ItemT] | None:
    return None if items is None else list(items)


class CvFields(TypedDict):
    cv_duration_ms: int | None
    validation_evidence_ref: str | None
    cv_platform: str | None
    cv_services_participating: int | None
    cv_services_blocked: int | None
    cv_services_total: int | None
    cv_service_issues: list[CvServiceIssue] | None
    mission_setup: str | None
    mission_steps: list[MissionStepEntry] | None


def _cv_fields(
    execution: StageExecution | None,
    evidence: ValidationEvidence | None,
    live: LiveValues,
) -> CvFields:
    if execution is None or evidence is None:
        settled = execution is not None
        return CvFields(
            cv_duration_ms=_duration(execution),
            validation_evidence_ref=_ref(execution, "cv", "validation_evidence"),
            cv_platform=None,
            cv_services_participating=None,
            cv_services_blocked=None,
            cv_services_total=None,
            cv_service_issues=None if settled else _listed(live.cv_service_issues),
            mission_setup=None if settled else live.mission_setup,
            mission_steps=None if settled else _listed(live.mission_steps),
        )
    services = evidence.services
    mission = evidence.mission
    reached = {} if mission is None else {s.requirement_id: s for s in mission.steps}
    return CvFields(
        cv_duration_ms=_duration(execution),
        validation_evidence_ref=_ref(execution, "cv", "validation_evidence"),
        cv_platform=evidence.platform,
        cv_services_participating=sum(
            item.participation in {"participated", "participated_loosened"}
            for item in services
        ),
        cv_services_blocked=sum(map(_blocked, services)),
        cv_services_total=len(services),
        cv_service_issues=[
            CvServiceIssue(
                service_id=item.service_id,
                participation=item.participation,
                reason=item.reason,
            )
            for item in services
            if isinstance(item, NonParticipatingService | UnevaluatedService)
            and not _blocked(item)
        ],
        mission_setup=None if mission is None else mission.setup,
        mission_steps=None
        if mission is None
        else [
            MissionStepEntry(
                id=step, status=reached[step].status if step in reached else None
            )
            for step in REQUIREMENTS
        ],
    )


def _blocked(service: ServiceParticipation) -> bool:
    return isinstance(service, UnevaluatedService) and service.reason == "blocked"


class CdFields(TypedDict):
    cd_duration_ms: int | None
    deployment_result_ref: str | None
    cd_placements_deployed: int | None
    cd_placements_blocked: int | None
    cd_placements_total: int | None
    placement_issues: list[PlacementIssue] | None


def _cd_fields(
    execution: StageExecution | None,
    result: DeploymentResult | None,
    live: LiveValues,
) -> CdFields:
    if execution is None or result is None:
        settled = execution is not None
        return CdFields(
            cd_duration_ms=_duration(execution),
            deployment_result_ref=_ref(execution, "cd", "deployment_result"),
            cd_placements_deployed=None,
            cd_placements_blocked=None,
            cd_placements_total=None,
            placement_issues=None if settled else _listed(live.placement_issues),
        )
    placements = result.placements
    issues: list[PlacementIssue] = []
    blocked = 0
    for item in placements:
        if isinstance(item, FailedPlacement):
            issues.append(
                PlacementIssue(
                    service_id=item.service_id,
                    host=item.host,
                    status="failed",
                    reason=item.k8s_reason or "other",
                )
            )
        elif isinstance(item, UndeployedPlacement) and item.reason == "blocked":
            blocked += 1
        elif isinstance(item, UndeployedPlacement):
            issues.append(
                PlacementIssue(
                    service_id=item.service_id,
                    host=item.host,
                    status="not_evaluated",
                    reason=item.reason,
                )
            )
    return CdFields(
        cd_duration_ms=_duration(execution),
        deployment_result_ref=_ref(execution, "cd", "deployment_result"),
        cd_placements_deployed=sum(item.status == "deployed" for item in placements),
        cd_placements_blocked=blocked,
        cd_placements_total=len(placements),
        placement_issues=issues,
    )
