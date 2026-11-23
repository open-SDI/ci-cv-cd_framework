"""Authoritative contracts for the language-neutral Stage-adapter seam."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Self, cast

from pydantic import (
    AwareDatetime,
    Field,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema  # noqa: TC002

from ._contracts import (
    ContractModel,
    NonBlank,
    RepositoryPath,
    Slug,
)
from ._git_input import validate_repository_path

ADAPTER_DESCRIPTOR_SCHEMA_VERSION = "sdi.adapter-descriptor/v1"
STAGE_PROFILE_SCHEMA_VERSION = "sdi.stage-profile/v1"
ADAPTER_REQUEST_SCHEMA_VERSION = "sdi.stage-adapter-request/v1"
ADAPTER_RESPONSE_SCHEMA_VERSION = "sdi.stage-adapter-response/v1"
ACCEPTED_ATTEMPT_SCHEMA_VERSION = "sdi.accepted-attempt-envelope/v1"
FIXTURE_CASE_SCHEMA_VERSION = "sdi.fixture-case/v1"
PROCESS_CONTRACT_VERSION = "sdi.stage-adapter-process/v1"
COMPOSITION_PROFILE_VERSION = "sdi.composition-stage-profile/v1"
IMAGE_BUILD_PROFILE_VERSION = "sdi.image-build-stage-profile/v1"
CV_PROFILE_VERSION = "sdi.cv-stage-profile/v1"
CD_PROFILE_VERSION = "sdi.cd-stage-profile/v1"

type StageName = Literal["composition", "image_build", "cv", "cd"]
type SlotOrigin = Literal["committed", "supplied"] | StageName
type ImplementationMode = Literal["fixture", "implemented", "not_implemented"]
type ExecutionConclusion = Literal["succeeded", "failed", "timed_out", "skipped"]
type AdapterExecutionConclusion = Literal["succeeded", "failed"]
type DomainOutcome = Literal["succeeded", "failed", "not_evaluated"]
Sha256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
SlotName = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$",
        max_length=63,
    ),
]
ExecutionId = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        ),
    ),
]
BoundedText = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=512),
]
ReasonCode = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^sdi\.[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$",
        max_length=127,
    ),
]
EnvironmentName = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[A-Z][A-Z0-9_]*$", max_length=63),
]
ImageReference = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$",
        max_length=255,
    ),
]

RESERVED_RESPONSE_PATH = "response.json"
RESERVED_ACCEPTED_ENVELOPE_PATH = "accepted-attempt.json"
DIAGNOSTIC_PATH = "diagnostic.txt"


@dataclass(frozen=True)
class Slot:
    """One Domain artifact a Stage may be granted, in canonical order.

    A committed slot comes from the run's committed inputs, a supplied slot from
    the operator's flags, and any other slot is the Domain output of its Stage.
    """

    name: str
    file_name: str
    media_type: str
    schema_version: str
    origin: SlotOrigin


SLOTS = (
    Slot(
        "run_request",
        "run-request.yaml",
        "application/yaml",
        "sdi.pipeline-integration-run-request/v1",
        "committed",
    ),
    Slot(
        "requirements_specification",
        "requirements-specification.md",
        "text/markdown",
        "sdi.mobility-requirements-specification/v1",
        "committed",
    ),
    Slot(
        "target_profile",
        "target-profile.yaml",
        "application/yaml",
        "sdi.target-execution-profile/v1",
        "committed",
    ),
    Slot(
        "service_repository",
        "service-repository.json",
        "application/json",
        "sdi.service-repository-manifest/v1",
        "supplied",
    ),
    Slot(
        "composition_settings",
        "composition-settings.json",
        "application/json",
        "sdi.composition-settings/v1",
        "supplied",
    ),
    Slot(
        "composition_blueprint",
        "composition-blueprint.json",
        "application/json",
        "sdi.composition-blueprint/v1",
        "composition",
    ),
    Slot(
        "deployment_schema",
        "deployment-schema.json",
        "application/json",
        "sdi.deployment-schema/v1",
        "composition",
    ),
    Slot(
        "image_build_result",
        "image-build-result.json",
        "application/json",
        "sdi.image-build-result/v1",
        "image_build",
    ),
    Slot(
        "validation_evidence",
        "validation-evidence.json",
        "application/json",
        "sdi.validation-evidence/v1",
        "cv",
    ),
    Slot(
        "deployment_result",
        "deployment-result.json",
        "application/json",
        "sdi.deployment-result/v1",
        "cd",
    ),
)
SLOT_BY_NAME = {slot.name: slot for slot in SLOTS}
SLOT_BY_SCHEMA = {slot.schema_version: slot for slot in SLOTS}
COMMITTED_SLOTS = tuple(slot for slot in SLOTS if slot.origin == "committed")
# Run-supplied inputs come from the operator's flags, not from committed run
# inputs or an earlier Stage, so no result provenance names their source.
SUPPLIED_INPUT_SLOTS = frozenset(
    slot.name for slot in SLOTS if slot.origin == "supplied"
)


@dataclass(frozen=True)
class EvidenceGrant:
    """Optional evidence of how a Stage reached its outcome.

    Evidence is never a Domain input to a later Stage, so it is not a slot.
    """

    name: str
    file_name: str
    media_type: str
    schema_version: str
    max_bytes: int


@dataclass(frozen=True)
class StageGrant:
    """Everything one Stage is granted; its profile is rendered from this.

    A Stage's Domain outputs are the slots it originates, each bounded by
    `domain_output_max_bytes`.
    """

    profile_version: str
    inputs: tuple[str, ...]
    work_limit_seconds: int
    domain_output_max_bytes: int
    diagnostic_max_bytes: int
    evidence: tuple[EvidenceGrant, ...] = ()


STAGE_GRANTS: dict[StageName, StageGrant] = {
    "composition": StageGrant(
        COMPOSITION_PROFILE_VERSION,
        (
            "run_request",
            "requirements_specification",
            "target_profile",
            "service_repository",
            "composition_settings",
        ),
        work_limit_seconds=10 * 60,
        domain_output_max_bytes=32768,
        diagnostic_max_bytes=16384,
        evidence=(
            EvidenceGrant(
                "composition_evidence",
                "composition-evidence.json",
                "application/json",
                "sdi.composition-evidence/v1",
                max_bytes=262144,
            ),
        ),
    ),
    "image_build": StageGrant(
        IMAGE_BUILD_PROFILE_VERSION,
        ("target_profile", "composition_blueprint", "deployment_schema"),
        work_limit_seconds=30 * 60,
        domain_output_max_bytes=32768,
        diagnostic_max_bytes=4096,
    ),
    "cv": StageGrant(
        CV_PROFILE_VERSION,
        (
            "requirements_specification",
            "target_profile",
            "composition_blueprint",
            "deployment_schema",
            "image_build_result",
        ),
        work_limit_seconds=45 * 60,
        domain_output_max_bytes=32768,
        diagnostic_max_bytes=4096,
    ),
    "cd": StageGrant(
        CD_PROFILE_VERSION,
        (
            "target_profile",
            "composition_blueprint",
            "deployment_schema",
            "image_build_result",
            "validation_evidence",
        ),
        work_limit_seconds=15 * 60,
        domain_output_max_bytes=32768,
        diagnostic_max_bytes=4096,
    ),
}
PROFILE_VERSIONS: dict[StageName, str] = {
    stage: grant.profile_version for stage, grant in STAGE_GRANTS.items()
}
EXPECTED_STAGE_INPUT_SLOTS: dict[StageName, tuple[str, ...]] = {
    stage: grant.inputs for stage, grant in STAGE_GRANTS.items()
}
EXPECTED_STAGE_OUTPUTS: dict[StageName, dict[str, tuple[str, str, str]]] = {
    stage: {
        slot.name: (f"outputs/{slot.file_name}", slot.media_type, slot.schema_version)
        for slot in SLOTS
        if slot.origin == stage
    }
    for stage in STAGE_GRANTS
}
EXPECTED_STAGE_EVIDENCE: dict[StageName, dict[str, tuple[str, str, str]]] = {
    stage: {
        evidence.name: (
            f"outputs/{evidence.file_name}",
            evidence.media_type,
            evidence.schema_version,
        )
        for evidence in grant.evidence
    }
    for stage, grant in STAGE_GRANTS.items()
}
MAX_ARTIFACT_PATH_BYTES = 512
MAX_ARTIFACT_COMPONENT_BYTES = 255
ASCII_CONTROL_LIMIT = 32
ASCII_DELETE = 127
IPV4_PATTERN = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])")
IPV6_PATTERN = re.compile(
    r"(?<![0-9a-f:])[0-9a-f]*:[0-9a-f:.]+(?![0-9a-f:])", re.IGNORECASE
)
POSIX_MACHINE_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._-])/(?:home|Users|tmp|private|mnt|media|var|etc|opt|srv|run|root|usr/local|workspace|workspaces)(?:/[^\s]*)?"
)
WINDOWS_MACHINE_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")
PRIVATE_HOSTNAME_PATTERN = re.compile(
    r"(?<![A-Za-z0-9.-])(?:[A-Za-z0-9-]+\.)+(?:internal|local|localhost)(?![A-Za-z0-9.-])",
    re.IGNORECASE,
)


def contains_sensitive_material(text: str) -> bool:
    """Identify endpoint and credential forms forbidden from durable evidence."""
    # Wire diagnostics have no standard redaction protocol. Reject credential
    # assignment forms directly and delegate address classification to ipaddress.
    lowered = text.casefold()
    if any(
        marker in lowered
        for marker in (
            "http://",
            "https://",
            "authorization:",
            "bearer ",
            "password=",
            "password:",
            "token=",
            "token:",
            "secret=",
            "secret:",
            "api_key=",
            "api-key=",
            "localhost",
            "::1",
        )
    ):
        return True
    if (
        POSIX_MACHINE_PATH_PATTERN.search(text)
        or WINDOWS_MACHINE_PATH_PATTERN.search(text)
        or PRIVATE_HOSTNAME_PATTERN.search(text)
    ):
        return True
    for candidate in [*IPV4_PATTERN.findall(text), *IPV6_PATTERN.findall(text)]:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address.is_private or address.is_loopback or address.is_link_local:
            return True
    return False


def contains_sensitive_contract_material(value: object) -> bool:
    """Recursively inspect one parsed JSON contract for forbidden strings."""
    if isinstance(value, str):
        return contains_sensitive_material(value)
    if isinstance(value, dict):
        mapping = cast("dict[object, object]", value)
        return any(
            contains_sensitive_contract_material(item) for item in mapping.values()
        )
    if isinstance(value, list):
        sequence = cast("list[object]", value)
        return any(contains_sensitive_contract_material(item) for item in sequence)
    return False


def _validate_confined_path(path: str) -> str:
    validate_repository_path(path)
    if len(path.encode()) > MAX_ARTIFACT_PATH_BYTES or any(
        len(part.encode()) > MAX_ARTIFACT_COMPONENT_BYTES for part in path.split("/")
    ):
        msg = "artifact path exceeds its bounded length"
        raise ValueError(msg)
    return path


def _require_unique(values: list[str], field_name: str) -> None:
    if len(values) != len(set(values)):
        msg = f"{field_name} values must be unique"
        raise ValueError(msg)


def _reject_null_reason(data: object) -> object:
    if isinstance(data, dict):
        values = cast("dict[object, object]", data)
        if "reason" in values and values["reason"] is None:
            msg = "reason must be omitted when absent"
            raise ValueError(msg)
    return cast("object", data)


def parse_wire_datetime(value: object) -> object:
    """Parse the RFC 3339 representation used by machine-produced JSON."""
    if not isinstance(value, str):
        return value
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        msg = "timestamp must be an RFC 3339 date-time"
        raise ValueError(msg) from error


def require_unique_paths(paths: list[str]) -> None:
    _require_unique(paths, "path")
    folded = [path.casefold() for path in paths]
    if len(folded) != len(set(folded)):
        msg = "artifact paths must not collide by case"
        raise ValueError(msg)
    parts = [path.split("/") for path in paths]
    for index, left in enumerate(parts):
        for right in parts[index + 1 :]:
            shorter, longer = sorted((left, right), key=len)
            if longer[: len(shorter)] == shorter:
                msg = "an artifact file path cannot contain another artifact file"
                raise ValueError(msg)


class Correlation(ContractModel):
    """Stable project correlation carried across one Stage attempt."""

    execution_id: ExecutionId
    stage: StageName
    attempt_number: PositiveInt


class Reason(ContractModel):
    """Bounded, sanitized process or Domain reason."""

    category: Literal["fixture", "input", "dependency", "tool", "adapter", "domain"]
    code: ReasonCode
    summary: BoundedText

    @field_validator("summary")
    @classmethod
    def reject_control_characters(cls, summary: str) -> str:
        if any(
            ord(character) < ASCII_CONTROL_LIMIT or ord(character) == ASCII_DELETE
            for character in summary
        ):
            msg = "reason summary must not contain control characters"
            raise ValueError(msg)
        if contains_sensitive_material(summary):
            msg = "reason summary must not contain credentials or private endpoints"
            raise ValueError(msg)
        return summary


class AdapterDescriptor(ContractModel):
    """Reviewed authority selecting one trusted Stage adapter."""

    schema_version: Literal["sdi.adapter-descriptor/v1"]
    stage: StageName
    implementation_mode: ImplementationMode
    image: ImageReference
    entrypoint: NonBlank
    process_contract_version: Literal["sdi.stage-adapter-process/v1"]
    stage_profile: RepositoryPath
    stage_profile_version: NonBlank
    agent_label: Slug
    secret_bindings: list[EnvironmentName]

    @field_validator("stage_profile")
    @classmethod
    def validate_profile_path(cls, path: str) -> str:
        return _validate_confined_path(path)

    @model_validator(mode="after")
    def validate_descriptor(self) -> Self:
        _require_unique(self.secret_bindings, "secret binding")
        if self.implementation_mode == "fixture" and self.secret_bindings:
            msg = "Fixture adapters cannot receive secret bindings"
            raise ValueError(msg)
        if self.stage_profile_version != PROFILE_VERSIONS[self.stage]:
            msg = f"{self.stage} descriptor must select its reviewed profile version"
            raise ValueError(msg)
        return self


class InputGrant(ContractModel):
    """One input file granted to an adapter."""

    slot: SlotName
    path: RepositoryPath
    media_type: NonBlank
    schema_version: NonBlank

    @field_validator("path")
    @classmethod
    def validate_path(cls, path: str) -> str:
        return _validate_confined_path(path)


class OutputGrant(ContractModel):
    """One bounded output file an adapter may publish."""

    slot: SlotName
    path: RepositoryPath
    media_type: NonBlank
    schema_version: NonBlank
    # A Domain output is required whenever the Domain outcome is not failed;
    # Stage evidence is optional and never a Domain input.
    role: Literal["domain_output", "stage_evidence"]
    max_bytes: PositiveInt

    @field_validator("path")
    @classmethod
    def validate_path(cls, path: str) -> str:
        return _validate_confined_path(path)


class DiagnosticGrant(ContractModel):
    """The sole bounded diagnostic file grant."""

    path: RepositoryPath
    media_type: Literal["text/plain; charset=utf-8"]
    max_bytes: PositiveInt

    @field_validator("path")
    @classmethod
    def validate_path(cls, path: str) -> str:
        return _validate_confined_path(path)


class StageProfile(ContractModel):
    """Strict least-privilege file and work grants for one Stage."""

    schema_version: Literal["sdi.stage-profile/v1"]
    profile_version: NonBlank
    stage: StageName
    work_limit_seconds: PositiveInt
    inputs: Annotated[list[InputGrant], Field(min_length=1)]
    outputs: Annotated[list[OutputGrant], Field(min_length=1)]
    diagnostic: DiagnosticGrant

    @model_validator(mode="after")
    def validate_grants(self) -> Self:
        input_slots = [item.slot for item in self.inputs]
        output_slots = [item.slot for item in self.outputs]
        _require_unique(input_slots, "input slot")
        _require_unique(output_slots, "output slot")
        require_unique_paths(
            [
                *(item.path for item in self.inputs),
                *(item.path for item in self.outputs),
                self.diagnostic.path,
                RESERVED_RESPONSE_PATH,
                RESERVED_ACCEPTED_ENVELOPE_PATH,
            ]
        )
        if self.profile_version != PROFILE_VERSIONS[self.stage]:
            msg = f"{self.stage} profile has an unsupported profile version"
            raise ValueError(msg)
        return self


def render_stage_profile(stage: StageName) -> StageProfile:
    """Render one Stage's profile from the registry as it stands now."""
    grant = STAGE_GRANTS[stage]
    # Read SLOTS now rather than the import-time SLOT_BY_NAME view.
    slots = {slot.name: slot for slot in SLOTS}
    return StageProfile.model_validate(
        {
            "schema_version": STAGE_PROFILE_SCHEMA_VERSION,
            "profile_version": grant.profile_version,
            "stage": stage,
            "work_limit_seconds": grant.work_limit_seconds,
            "inputs": [
                {
                    "slot": name,
                    "path": slots[name].file_name,
                    "media_type": slots[name].media_type,
                    "schema_version": slots[name].schema_version,
                }
                for name in grant.inputs
            ],
            "outputs": [
                *(
                    {
                        "slot": slot.name,
                        "path": f"outputs/{slot.file_name}",
                        "media_type": slot.media_type,
                        "schema_version": slot.schema_version,
                        "role": "domain_output",
                        "max_bytes": grant.domain_output_max_bytes,
                    }
                    for slot in SLOTS
                    if slot.origin == stage
                ),
                *(
                    {
                        "slot": evidence.name,
                        "path": f"outputs/{evidence.file_name}",
                        "media_type": evidence.media_type,
                        "schema_version": evidence.schema_version,
                        "role": "stage_evidence",
                        "max_bytes": evidence.max_bytes,
                    }
                    for evidence in grant.evidence
                ),
            ],
            "diagnostic": {
                "path": DIAGNOSTIC_PATH,
                "media_type": "text/plain; charset=utf-8",
                "max_bytes": grant.diagnostic_max_bytes,
            },
        }
    )


class DeclaredInput(InputGrant):
    """An input grant bound to exact accepted bytes."""

    byte_size: NonNegativeInt
    sha256: Sha256


class AdapterRequest(ContractModel):
    """Complete least-privilege request for one adapter invocation."""

    schema_version: Literal["sdi.stage-adapter-request/v1"]
    correlation: Correlation
    work_limit_seconds: PositiveInt
    inputs: Annotated[list[DeclaredInput], Field(min_length=1)]
    outputs: Annotated[list[OutputGrant], Field(min_length=1)]
    diagnostic: DiagnosticGrant

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        _require_unique([item.slot for item in self.inputs], "input slot")
        _require_unique([item.slot for item in self.outputs], "output slot")
        require_unique_paths(
            [
                *(item.path for item in self.inputs),
                *(item.path for item in self.outputs),
                self.diagnostic.path,
                RESERVED_RESPONSE_PATH,
                RESERVED_ACCEPTED_ENVELOPE_PATH,
            ]
        )
        return self


class DiagnosticResponse(ContractModel):
    """Adapter-reported diagnostic publication facts."""

    present: bool
    truncated: bool

    @model_validator(mode="after")
    def validate_truncation(self) -> Self:
        if self.truncated and not self.present:
            msg = "an absent diagnostic cannot be truncated"
            raise ValueError(msg)
        return self


class AdapterResponse(ContractModel):
    """Untrusted candidate response published by an adapter."""

    schema_version: Literal["sdi.stage-adapter-response/v1"]
    correlation: Correlation
    execution_conclusion: AdapterExecutionConclusion
    domain_outcome: DomainOutcome
    reason: Reason | SkipJsonSchema[None] = None
    consumed_inputs: list[SlotName]
    produced_outputs: list[SlotName]
    diagnostic: DiagnosticResponse

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_absent_reason(cls, data: object) -> object:
        return _reject_null_reason(data)

    @model_validator(mode="after")
    def validate_slots(self) -> Self:
        _require_unique(self.consumed_inputs, "consumed input slot")
        _require_unique(self.produced_outputs, "produced output slot")
        if self.execution_conclusion == "failed" and (
            self.domain_outcome != "not_evaluated" or self.produced_outputs
        ):
            msg = "failed adapter execution cannot claim Domain output"
            raise ValueError(msg)
        if self.execution_conclusion == "failed" and self.reason is None:
            msg = "failed adapter execution requires a typed reason"
            raise ValueError(msg)
        if self.execution_conclusion not in {"succeeded", "failed"}:
            msg = "an adapter may report only succeeded or failed execution"
            raise ValueError(msg)
        if self.domain_outcome == "failed" and (
            self.execution_conclusion != "succeeded"
            or self.reason is None
            or self.reason.category != "domain"
        ):
            msg = "a negative Domain outcome requires a successful execution and reason"
            raise ValueError(msg)
        return self


class AcceptedInput(ContractModel):
    """Exact declared input accepted for this attempt."""

    slot: SlotName
    source_path: RepositoryPath
    schema_version: NonBlank
    byte_size: NonNegativeInt
    sha256: Sha256

    @field_validator("source_path")
    @classmethod
    def validate_source_path(cls, path: str) -> str:
        return _validate_confined_path(path)


class AcceptedDomainFile(ContractModel):
    """One schema-valid Domain file accepted by the integration runtime."""

    role: Literal["domain_output"]
    slot: SlotName
    path: RepositoryPath
    media_type: NonBlank
    schema_version: NonBlank
    byte_size: NonNegativeInt
    sha256: Sha256


class AcceptedEvidenceFile(ContractModel):
    """One schema-valid record of how an implemented Stage reached its outcome."""

    role: Literal["stage_evidence"]
    slot: SlotName
    path: RepositoryPath
    media_type: NonBlank
    schema_version: NonBlank
    byte_size: NonNegativeInt
    sha256: Sha256


class AcceptedDiagnosticFile(ContractModel):
    """One bounded sanitized diagnostic accepted by the integration runtime."""

    role: Literal["diagnostic"]
    slot: Literal["diagnostic"]
    path: RepositoryPath
    media_type: Literal["text/plain; charset=utf-8"]
    byte_size: NonNegativeInt
    sha256: Sha256
    truncated: bool


type AcceptedFile = Annotated[
    AcceptedDomainFile | AcceptedEvidenceFile | AcceptedDiagnosticFile,
    Field(discriminator="role"),
]


class ProcessFacts(ContractModel):
    """Authoritative process observations recorded by the runtime."""

    started_at: AwareDatetime
    finished_at: AwareDatetime
    duration_ms: NonNegativeInt
    termination: Literal["exited", "signaled", "timed_out", "lost"]
    exit_code: int | None = None
    signal: PositiveInt | None = None

    @field_validator("started_at", "finished_at", mode="before")
    @classmethod
    def parse_timestamps(cls, value: object) -> object:
        return parse_wire_datetime(value)

    @model_validator(mode="after")
    def validate_timing(self) -> Self:
        if self.finished_at < self.started_at:
            msg = "process timestamps are not monotonic"
            raise ValueError(msg)
        expected_duration = int(
            (self.finished_at - self.started_at).total_seconds() * 1000
        )
        if self.duration_ms != expected_duration:
            msg = "process duration does not match its immutable timestamps"
            raise ValueError(msg)
        if self.termination == "exited" and (
            self.exit_code is None or self.exit_code < 0 or self.signal is not None
        ):
            msg = "an exited process requires a nonnegative exit code"
            raise ValueError(msg)
        if self.termination == "signaled" and (
            self.signal is None or self.exit_code is not None
        ):
            msg = "a signaled process requires only its signal number"
            raise ValueError(msg)
        if self.termination in {"timed_out", "lost"} and (
            self.exit_code is not None or self.signal is not None
        ):
            msg = f"a {self.termination} process cannot claim an exit status"
            raise ValueError(msg)
        return self


class AcceptedAttemptEnvelope(ContractModel):
    """Transactional record created only after whole-bundle acceptance."""

    schema_version: Literal["sdi.accepted-attempt-envelope/v1"]
    correlation: Correlation
    lifecycle_state: Literal["completed", "skipped"]
    execution_conclusion: ExecutionConclusion
    implementation_mode: ImplementationMode
    domain_outcome: DomainOutcome
    reason: Reason | SkipJsonSchema[None] = None
    adapter_response_accepted: bool
    process: ProcessFacts | SkipJsonSchema[None] = None
    accepted_inputs: list[AcceptedInput]
    accepted_files: list[AcceptedFile]

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_absent_reason(cls, data: object) -> object:
        return _reject_null_reason(data)

    @model_validator(mode="after")
    def preserve_settled_semantics(self) -> Self:  # noqa: C901, PLR0912
        if self.lifecycle_state == "skipped" and (
            self.execution_conclusion != "skipped"
            or self.domain_outcome != "not_evaluated"
            or self.process is not None
            or self.adapter_response_accepted
            or self.accepted_inputs
            or self.accepted_files
            or self.reason is None
        ):
            msg = "a skipped attempt cannot claim execution or accepted evidence"
            raise ValueError(msg)
        if (
            self.lifecycle_state == "completed"
            and self.execution_conclusion == "skipped"
        ):
            msg = "a completed attempt cannot have a skipped conclusion"
            raise ValueError(msg)
        if self.lifecycle_state == "completed" and self.process is None:
            msg = "a completed implemented or Fixture attempt requires process facts"
            raise ValueError(msg)
        if self.lifecycle_state == "completed" and not self.accepted_inputs:
            msg = "a completed attempt requires its exact accepted inputs"
            raise ValueError(msg)
        if (
            self.lifecycle_state == "skipped"
            and self.reason is not None
            and (self.reason.category != "dependency")
        ):
            msg = "a skipped attempt requires a dependency reason"
            raise ValueError(msg)
        if self.execution_conclusion in {"failed", "timed_out"} and (
            self.domain_outcome != "not_evaluated" or self.reason is None
        ):
            msg = "failed or timed-out execution requires a typed unevaluated reason"
            raise ValueError(msg)
        if self.execution_conclusion == "timed_out" and (
            self.process is None or self.process.termination != "timed_out"
        ):
            msg = "timed-out execution requires timed-out process facts"
            raise ValueError(msg)
        if not self.adapter_response_accepted and self.accepted_files:
            msg = "an unaccepted adapter response cannot contribute files"
            raise ValueError(msg)
        if self.execution_conclusion == "succeeded" and not (
            self.adapter_response_accepted
            and self.process is not None
            and self.process.termination == "exited"
            and self.process.exit_code == 0
        ):
            msg = "successful execution requires an accepted zero-exit response"
            raise ValueError(msg)
        if self.adapter_response_accepted and not (
            self.process is not None
            and self.process.termination == "exited"
            and self.process.exit_code == 0
        ):
            msg = "an accepted adapter response requires zero-exit process facts"
            raise ValueError(msg)
        if self.adapter_response_accepted and self.execution_conclusion in {
            "timed_out",
            "skipped",
        }:
            msg = "timeout and skip conclusions are runtime-owned"
            raise ValueError(msg)
        if self.domain_outcome == "failed" and (
            self.execution_conclusion != "succeeded"
            or self.reason is None
            or self.reason.category != "domain"
        ):
            msg = "a negative Domain outcome requires a successful execution and reason"
            raise ValueError(msg)
        if self.implementation_mode == "fixture" and self.domain_outcome == "succeeded":
            msg = "Fixture attempt must preserve its evidence limits"
            raise ValueError(msg)
        if (
            self.implementation_mode == "fixture"
            and self.execution_conclusion == "succeeded"
            and self.domain_outcome == "not_evaluated"
            and (self.reason is None or self.reason.category != "fixture")
        ):
            msg = "successful Fixture execution requires its evidence-limit reason"
            raise ValueError(msg)
        if self.implementation_mode == "not_implemented" and not (
            self.lifecycle_state == "skipped" and self.execution_conclusion == "skipped"
        ):
            msg = "a Stage without an implementation must be explicitly skipped"
            raise ValueError(msg)
        return self


class FixtureInputMatch(ContractModel):
    """One exact input digest participating in Fixture selection."""

    slot: SlotName
    byte_size: NonNegativeInt
    sha256: Sha256


class FixtureOutput(ContractModel):
    """One immutable output payload owned by a Fixture case."""

    slot: SlotName
    source_path: RepositoryPath
    byte_size: NonNegativeInt
    sha256: Sha256

    @field_validator("source_path")
    @classmethod
    def validate_source_path(cls, path: str) -> str:
        return _validate_confined_path(path)


class FixtureDiagnostic(ContractModel):
    """One immutable sanitized Fixture diagnostic."""

    source_path: RepositoryPath
    byte_size: NonNegativeInt
    sha256: Sha256

    @field_validator("source_path")
    @classmethod
    def validate_source_path(cls, path: str) -> str:
        return _validate_confined_path(path)


class FixtureCase(ContractModel):
    """Exact-digest-selected deterministic pipeline-interface behavior."""

    schema_version: Literal["sdi.fixture-case/v1"]
    case_id: Slug
    stage: StageName
    match_inputs: Annotated[list[FixtureInputMatch], Field(min_length=1)]
    behavior: Literal[
        "response",
        "absent_response",
        "crash",
        "timeout",
        "malformed_response",
        "undeclared_output",
        "unsafe_output",
        "oversize_output",
        "missing_output",
        "schema_mismatch",
        "response_mismatch",
    ] = "response"
    execution_conclusion: AdapterExecutionConclusion
    domain_outcome: DomainOutcome
    reason: Reason
    outputs: list[FixtureOutput]
    diagnostic: FixtureDiagnostic

    @model_validator(mode="after")
    def validate_case(self) -> Self:
        _require_unique([item.slot for item in self.match_inputs], "Fixture input slot")
        _require_unique([item.slot for item in self.outputs], "Fixture output slot")
        if (
            self.behavior == "response"
            and self.execution_conclusion == "succeeded"
            and self.domain_outcome != "failed"
            and not self.outputs
        ):
            msg = "a successful Fixture case must declare outputs"
            raise ValueError(msg)
        if self.domain_outcome == "failed" and self.outputs:
            msg = "a negative Domain Fixture case cannot declare Domain output"
            raise ValueError(msg)
        if self.execution_conclusion not in {"succeeded", "failed"}:
            msg = "a Fixture adapter response may report only succeeded or failed"
            raise ValueError(msg)
        if self.execution_conclusion == "failed" and self.outputs:
            msg = "a failed Fixture case cannot declare Domain output"
            raise ValueError(msg)
        if self.behavior != "response" and (
            self.execution_conclusion != "failed"
            or self.domain_outcome != "not_evaluated"
            or self.outputs
            or self.reason.category != "fixture"
        ):
            msg = "non-response Fixture behavior must describe unevaluated failure"
            raise ValueError(msg)
        return self
