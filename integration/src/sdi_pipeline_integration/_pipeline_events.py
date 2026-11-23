"""Pipeline events: the row model, the message and line renderers, the writer."""

from __future__ import annotations

import contextlib
import json
import os
import string
import sys
from datetime import UTC
from typing import TYPE_CHECKING, Annotated, Literal, cast

from pydantic import ConfigDict, Field, NonNegativeInt, StringConstraints
from pydantic.fields import FieldInfo  # noqa: TC002

from ._contracts import CombinationId, ContractModel, ScenarioId, Slug, TestcaseId
from ._cv_result import REQUIREMENTS
from ._domain_contracts import (
    DEPLOYMENT_GAPS,
    DID_NOT_PARTICIPATE_REASONS,
    FAILED_IMAGE_REASONS,
    K8S_REASONS,
    UNEVALUATED_REASONS,
)
from ._pipeline_event_catalogue import (
    MESSAGE_MAX_BYTES,
    MISSION_STATUSES,
    SLOT_TYPES,
    STEP_BY_CODE,
    STEP_CODES,
    PipelineStatus,
    SlotName,
    SlotValue,
    Step,
    StepCode,
    takes_slots,
    template_slots,
)
from ._stage_contracts import ExecutionId, ReasonCode  # noqa: TC001
from ._stage_registry import STAGES, StageName

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from pydantic.config import JsonDict

SCHEMA_VERSION = "sdi.pipeline-event/v1"
PRISM_PREFIX = "PRISM_EVENT "
SDI_PREFIX = "[sdi] "
# GitHub Actions reads "##[" in a log line as a workflow command (spec 2).
BRACKET_AFTER_HASHES = "##\\u005b"
INTERNAL_ERROR_PREFIX = "sdi-integration: internal error: "

type EventScope = Literal["run", "single_stage"]


def _allow_unknown_properties(schema: JsonDict) -> None:
    schema["additionalProperties"] = True


class EventModel(ContractModel):
    """Closed in Python, open in the published schema."""

    model_config = ConfigDict(json_schema_extra=_allow_unknown_properties)


def open_code(*known: tuple[str, ...]) -> FieldInfo:
    """`Annotated` metadata for a code-like string whose known values are `known`."""
    examples = [value for group in known for value in group]
    return cast(
        "FieldInfo",
        Field(examples=examples, pattern=r"^[A-Za-z][A-Za-z0-9_.+/-]*$", max_length=63),
    )


EmittedAt = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$",
    ),
]
StageRef = Annotated[
    str,
    Field(
        pattern=r"^stages/[a-z][a-z0-9-]*/[a-z][a-z0-9.-]*$",
        max_length=255,
        examples=[
            "stages/composition/composition-blueprint.json",
            "stages/composition/deployment-schema.json",
            "stages/image-build/image-build-result.json",
            "stages/cv/validation-evidence.json",
            "stages/cd/deployment-result.json",
        ],
    ),
]
EventImageRef = Annotated[
    str,
    Field(
        max_length=255,
        examples=[
            (
                "registry:5000/sdi/navigation:navigation-arm64-"
                "6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b"
            )
        ],
    ),
]
EventImageDigest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
EventPlatform = Annotated[
    str, Field(pattern=r"^linux/[a-z0-9]+$", examples=["linux/amd64", "linux/arm64"])
]
Message = Annotated[str, Field(min_length=1, max_length=MESSAGE_MAX_BYTES)]


class ImageFailure(EventModel):
    """One `failed` image record."""

    service_id: Slug
    reason: Annotated[str, open_code(FAILED_IMAGE_REASONS)]


class CvServiceIssue(EventModel):
    """One service that did not participate or was not evaluated."""

    service_id: Slug
    participation: Annotated[str, open_code(("did_not_participate", "not_evaluated"))]
    reason: Annotated[
        str,
        open_code(
            DID_NOT_PARTICIPATE_REASONS,
            tuple(reason for reason in UNEVALUATED_REASONS if reason != "blocked"),
        ),
    ]


class MissionStepEntry(EventModel):
    """One S-04 mission step; `status` is null until it is reported."""

    id: Annotated[str, open_code(REQUIREMENTS)]
    status: Annotated[str, open_code((*MISSION_STATUSES, "not_reached"))] | None


class PlacementIssue(EventModel):
    """One placement that is neither deployed nor `blocked`.

    `reason` is a Kubernetes reason for a `failed` placement and a deployment
    gap for a `not_evaluated` one.
    """

    service_id: Slug
    host: Slug | None
    status: Annotated[str, open_code(("failed", "not_evaluated"))]
    reason: Annotated[
        str,
        open_code(
            K8S_REASONS, tuple(gap for gap in DEPLOYMENT_GAPS if gap != "blocked")
        ),
    ]


class PipelineEventRow(EventModel):
    """One `PRISM_EVENT` line: a full snapshot of the combination's row.

    Field order is the wire key order. No field has a default: a builder states
    every value, and a parsed line carries every key. `stage`, `target`,
    `architecture`, the statuses and `platform_status` are closed enums; every
    other code-like value is an open string whose schema lists `examples`.
    """

    schema_version: Literal["sdi.pipeline-event/v1"]
    emitted_at: EmittedAt
    execution_id: ExecutionId
    final: bool
    stage: StageName | None
    step: Annotated[str, open_code(STEP_CODES)]
    message: Message
    composition_id: CombinationId
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    profile_id: Slug
    target: Literal["SDV", "SDR"] | None
    architecture: Literal["amd64", "arm64"] | None
    composition_status: PipelineStatus
    image_build_status: PipelineStatus
    ci_status: PipelineStatus
    cv_status: PipelineStatus
    cd_status: PipelineStatus
    failure_reason: ReasonCode | None
    ci_duration_ms: NonNegativeInt | None
    blueprint_ref: StageRef | None
    deployment_schema_ref: StageRef | None
    image_build_result_ref: StageRef | None
    image_ref: list[EventImageRef] | None
    image_digest: list[EventImageDigest] | None
    image_archive: None
    images_built: NonNegativeInt | None
    images_total: NonNegativeInt | None
    image_failures: list[ImageFailure] | None
    platform_status: Literal["MATCH", "MISMATCH"] | None
    detected_platform: EventPlatform | None
    cv_duration_ms: NonNegativeInt | None
    validation_evidence_ref: StageRef | None
    cv_platform: EventPlatform | None
    cv_services_participating: NonNegativeInt | None
    cv_services_blocked: NonNegativeInt | None
    cv_services_total: NonNegativeInt | None
    cv_service_issues: list[CvServiceIssue] | None
    mission_setup: Annotated[str, open_code(("passed", "timed_out"))] | None
    mission_steps: list[MissionStepEntry] | None
    cd_duration_ms: NonNegativeInt | None
    deployment_result_ref: StageRef | None
    cd_placements_deployed: NonNegativeInt | None
    cd_placements_blocked: NonNegativeInt | None
    cd_placements_total: NonNegativeInt | None
    placement_issues: list[PlacementIssue] | None

    def stage_status(self, stage: StageName) -> PipelineStatus:
        """Return the Stage's own status."""
        match stage:
            case "composition":
                return self.composition_status
            case "image_build":
                return self.image_build_status
            case "cv":
                return self.cv_status
            case "cd":
                return self.cd_status


_CI_SEVERITY: dict[PipelineStatus, int] = {
    "SUCCEEDED": 0,
    "NOT_EVALUATED": 1,
    "SKIPPED": 2,
    "FAILED": 3,
}


def derive_ci_status(
    composition: PipelineStatus,
    image_build: PipelineStatus,
    *,
    scope: EventScope,
) -> PipelineStatus:
    """`ci_status` from the two CI halves."""
    halves = (composition, image_build)
    if "RUNNING" in halves:
        return "RUNNING"
    terminal: list[PipelineStatus] = [
        status for status in halves if status != "PENDING"
    ]
    if not terminal:
        return "PENDING"
    if len(terminal) == 1 and scope == "run":
        return "RUNNING"
    return max(terminal, key=lambda status: _CI_SEVERITY[status])


def format_emitted_at(moment: datetime) -> str:
    """UTC, RFC 3339, milliseconds, `Z` suffix."""
    if moment.utcoffset() is None:
        msg = "emitted_at needs an aware datetime, not a naive one"
        raise ValueError(msg)
    utc = moment.astimezone(UTC).isoformat(timespec="milliseconds")
    return f"{utc.removesuffix('+00:00')}Z"


def _escape(char: str) -> str:
    code = ord(char)
    if code > 0xFFFF:  # noqa: PLR2004
        code -= 0x10000
        units = (0xD800 | (code >> 10), 0xDC00 | (code & 0x3FF))
    else:
        units = (code,)
    return "".join(f"\\u{unit:04x}" for unit in units)


def console_safe(text: str) -> str:
    """Printable ASCII with every other character as a backslash-u escape."""
    escaped = "".join(
        "\\\\" if char == "\\" else char if " " <= char <= "~" else _escape(char)
        for char in text
    )
    return escaped.replace("##[", BRACKET_AFTER_HASHES)


def _strip_line_breaks(value: str) -> str:
    return "".join(value.splitlines())


def render_message(
    step: StepCode,
    *,
    stage: StageName | None,
    slots: Mapping[SlotName, SlotValue],
) -> str:
    """Fill the step's template whose slots are `slots` plus `{Stage}`."""
    entry = STEP_BY_CODE[step]
    return _render(entry, entry.form_for(slots.keys()), stage=stage, slots=slots)


def render_heartbeat(
    step: StepCode,
    *,
    stage: StageName,
    slots: Mapping[SlotName, SlotValue],
) -> str:
    """Render the heartbeat message of a long step, as `render_message` does."""
    entry = STEP_BY_CODE[step]
    if entry.heartbeat is None:
        msg = f"step {step} has no heartbeat"
        raise ValueError(msg)
    template = entry.heartbeat if takes_slots(entry.heartbeat, slots.keys()) else None
    return _render(entry, template, stage=stage, slots=slots)


def _render(
    entry: Step,
    template: str | None,
    *,
    stage: StageName | None,
    slots: Mapping[SlotName, SlotValue],
) -> str:
    if not entry.allows_stage(stage):
        msg = f"step {entry.code} does not appear on {stage or 'run-level'} lines"
        raise ValueError(msg)
    values: dict[SlotName, str] = {
        name: _strip_line_breaks(str(value)) for name, value in slots.items()
    }
    if template is None:
        msg = f"step {entry.code} has no form taking slots {sorted(values)}"
        raise ValueError(msg)
    if stage is not None and "Stage" in template_slots(template):
        values["Stage"] = STAGES[stage].event_label
    optional = frozenset(name for name in values if SLOT_TYPES[name].optional)
    for left_out in (frozenset[SlotName](), optional):
        text = string.Formatter().vformat(
            template,
            (),
            {
                name: "-" if name in left_out else value
                for name, value in values.items()
            },
        )
        if len(console_safe(text)) <= MESSAGE_MAX_BYTES:
            break
    if left_out:
        error = (
            f"{INTERNAL_ERROR_PREFIX}{entry.code} message over {MESSAGE_MAX_BYTES} "
            f"bytes; left out {', '.join(sorted(left_out))}\n"
        )
        _write_stderr(error.encode())
    return text


def render_sdi_line(row: PipelineEventRow) -> str:
    """Render the `[sdi]` line for a row, without its LF."""
    if row.stage is None:
        text = f"{SDI_PREFIX}{row.composition_id} RUN > {row.step}  {row.message}"
    else:
        text = (
            f"{SDI_PREFIX}{row.composition_id} {STAGES[row.stage].event_tag} > "
            f"{row.step}  {row.stage_status(row.stage)}  {row.message}"
        )
    return console_safe(text)


def render_prism_line(row: PipelineEventRow) -> str:
    """Render the `PRISM_EVENT` line for a row, without its LF."""
    payload = json.dumps(
        row.model_dump(mode="json"),
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return f"{PRISM_PREFIX}{payload}".replace("##[", BRACKET_AFTER_HASHES)


def encode_event(row: PipelineEventRow) -> bytes:
    """Render both lines as they are written."""
    return f"{render_sdi_line(row)}\n{render_prism_line(row)}\n".encode("ascii")


def write_event(row: PipelineEventRow, *, fd: int = 2) -> None:
    """Write one line pair to stderr in one `os.write`, ignoring failures."""
    _write_stderr(encode_event(row), fd=fd)


def _write_stderr(data: bytes, *, fd: int = 2) -> None:
    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stderr.flush()
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
        except OSError:
            return
        if written <= 0:
            return
        view = view[written:]
