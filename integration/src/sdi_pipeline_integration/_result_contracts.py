"""Authoritative contract for one complete Pipeline integration result."""

from __future__ import annotations

from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    Field,
    NonNegativeInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from ._contracts import (
    CombinationId,
    ContractModel,
    NonBlank,
    RepositoryIdentity,
    RepositoryPath,
    ScenarioId,
    Slug,
    TestcaseId,
)
from ._git_input import validate_repository_path
from ._stage_contracts import (
    EXPECTED_STAGE_INPUT_SLOTS,
    EXPECTED_STAGE_OUTPUTS,
    AcceptedAttemptEnvelope,
    ExecutionId,
    Sha256,
    SlotName,
    StageName,
    parse_wire_datetime,
    require_unique_paths,
)

PIPELINE_RESULT_SCHEMA_VERSION = "sdi.pipeline-integration-result/v1"
PIPELINE_RESULT_PATH = "pipeline-integration-result.json"
FIXTURE_NOTICE = (
    "Deterministic pipeline-interface Fixtures may have simulated outcomes; no "
    "real Domain capability, Validation verdict, deployment outcome, or KPI was "
    "evaluated."
)
EXPECTED_STAGES: tuple[StageName, ...] = ("composition", "image_build", "cv", "cd")
GitCommitSha = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[0-9a-f]{40}$"),
]
SmallDomainFileSize = Annotated[NonNegativeInt, Field(le=32768)]
BoundedDiagnosticSize = Annotated[NonNegativeInt, Field(le=4096)]


def archive_path(stage: StageName, accepted_path: str) -> str:
    """Return the fixed Stage-qualified archive path for one accepted file."""
    filename = accepted_path.removeprefix("outputs/")
    return f"stages/{stage.replace('_', '-')}/{filename}"


class ResultInputProvenance(ContractModel):
    """Exact committed bytes that identified the Pipeline integration run."""

    path: RepositoryPath
    schema_version: NonBlank
    byte_size: NonNegativeInt
    sha256: Sha256

    @field_validator("path")
    @classmethod
    def validate_path(cls, path: str) -> str:
        validate_repository_path(path)
        return path


class BundleDomainArtifact(ContractModel):
    """One schema-valid Domain output included in the archive candidate."""

    role: Literal["domain_output"]
    stage: StageName
    slot: SlotName
    path: RepositoryPath
    media_type: NonBlank
    schema_version: NonBlank
    byte_size: SmallDomainFileSize
    sha256: Sha256

    @field_validator("path")
    @classmethod
    def validate_path(cls, path: str) -> str:
        validate_repository_path(path)
        return path


class BundleDiagnosticArtifact(ContractModel):
    """One bounded sanitized diagnostic included in the archive candidate."""

    role: Literal["diagnostic"]
    stage: StageName
    slot: Literal["diagnostic"]
    path: RepositoryPath
    media_type: Literal["text/plain; charset=utf-8"]
    byte_size: BoundedDiagnosticSize
    sha256: Sha256
    truncated: bool

    @field_validator("path")
    @classmethod
    def validate_path(cls, path: str) -> str:
        validate_repository_path(path)
        return path


type BundleArtifact = Annotated[
    BundleDomainArtifact | BundleDiagnosticArtifact,
    Field(discriminator="role"),
]


class PipelineIntegrationResult(ContractModel):
    """Complete Fixture result and inventory for one supported combination."""

    schema_version: Literal["sdi.pipeline-integration-result/v1"]
    execution_id: ExecutionId
    repository: RepositoryIdentity
    requested_ref: Literal["refs/heads/main"]
    resolved_commit_sha: GitCommitSha
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    combination_id: CombinationId
    profile_id: Slug
    started_at: AwareDatetime
    finished_at: AwareDatetime
    duration_ms: NonNegativeInt
    input_provenance: Annotated[
        list[ResultInputProvenance], Field(min_length=3, max_length=3)
    ]
    attempts: Annotated[
        list[AcceptedAttemptEnvelope], Field(min_length=4, max_length=4)
    ]
    artifacts: list[BundleArtifact]
    fixture_notice: Literal[
        "Deterministic pipeline-interface Fixtures may have simulated outcomes; no "
        "real Domain capability, Validation verdict, deployment outcome, or KPI was "
        "evaluated."
    ]
    kpi_evaluation: Literal["not_evaluated"]

    @field_validator("started_at", "finished_at", mode="before")
    @classmethod
    def parse_timestamps(cls, value: object) -> object:
        return parse_wire_datetime(value)

    @model_validator(mode="after")
    def validate_complete_result(self) -> Self:  # noqa: C901, PLR0912, PLR0915
        stages = tuple(attempt.correlation.stage for attempt in self.attempts)
        if stages != EXPECTED_STAGES:
            msg = "result must contain one terminal attempt in reviewed Stage order"
            raise ValueError(msg)
        if any(
            attempt.correlation.execution_id != self.execution_id
            or attempt.correlation.attempt_number != 1
            for attempt in self.attempts
        ):
            msg = "result attempts must share identity and be terminal first attempts"
            raise ValueError(msg)
        if self.finished_at < self.started_at:
            msg = "result timestamps are not monotonic"
            raise ValueError(msg)
        expected_duration = int(
            (self.finished_at - self.started_at).total_seconds() * 1000
        )
        if self.duration_ms != expected_duration:
            msg = "result duration does not match its immutable timestamps"
            raise ValueError(msg)
        processes = [attempt.process for attempt in self.attempts if attempt.process]
        if any(
            process.started_at < self.started_at
            or process.finished_at > self.finished_at
            for process in processes
        ):
            msg = "result timestamps do not span its authoritative process facts"
            raise ValueError(msg)
        for previous, current in pairwise(processes):
            if current.started_at < previous.finished_at:
                msg = "result attempts are not sequential"
                raise ValueError(msg)

        input_schemas = [item.schema_version for item in self.input_provenance]
        if input_schemas != [
            "sdi.pipeline-integration-run-request/v1",
            "sdi.mobility-requirements-specification/v1",
            "sdi.target-execution-profile/v1",
        ]:
            msg = "result input provenance is not the identified input set"
            raise ValueError(msg)
        require_unique_paths([item.path for item in self.input_provenance])

        sources: dict[str, tuple[object, ...]] = {
            "run_request": (
                self.input_provenance[0].path,
                self.input_provenance[0].schema_version,
                self.input_provenance[0].byte_size,
                self.input_provenance[0].sha256,
                None,
                None,
            ),
            "requirements_specification": (
                self.input_provenance[1].path,
                self.input_provenance[1].schema_version,
                self.input_provenance[1].byte_size,
                self.input_provenance[1].sha256,
                None,
                None,
            ),
            "target_profile": (
                self.input_provenance[2].path,
                self.input_provenance[2].schema_version,
                self.input_provenance[2].byte_size,
                self.input_provenance[2].sha256,
                None,
                None,
            ),
        }
        blocking_stage: StageName | None = None
        for attempt in self.attempts:
            stage = attempt.correlation.stage
            if blocking_stage is not None:
                if attempt.lifecycle_state != "skipped":
                    msg = (
                        f"{stage} must be skipped after blocking "
                        f"{blocking_stage} outcome"
                    )
                    raise ValueError(msg)
                continue
            if attempt.lifecycle_state == "skipped":
                if attempt.reason is None or attempt.reason.code not in {
                    "sdi.stage.not-implemented",
                    "sdi.dependency.fixture-evidence",
                    "sdi.run.deadline-exceeded",
                }:
                    msg = f"{stage} has no typed reason for its initial skip"
                    raise ValueError(msg)
                blocking_stage = stage
                continue
            if [item.slot for item in attempt.accepted_inputs] != list(
                EXPECTED_STAGE_INPUT_SLOTS[stage]
            ):
                msg = f"{stage} attempt inputs do not match the reviewed graph"
                raise ValueError(msg)
            for accepted in attempt.accepted_inputs:
                if accepted.slot not in sources:
                    msg = f"{stage} attempt input has no accepted source"
                    raise ValueError(msg)
                if (
                    accepted.source_path,
                    accepted.schema_version,
                    accepted.byte_size,
                    accepted.sha256,
                ) != sources[accepted.slot][:4]:
                    msg = f"{stage} attempt input does not match its accepted source"
                    raise ValueError(msg)
                source_mode = sources[accepted.slot][4]
                source_outcome = sources[accepted.slot][5]
                if (
                    attempt.implementation_mode == "implemented"
                    and source_mode == "fixture"
                    and source_outcome == "not_evaluated"
                ):
                    msg = "implemented attempt consumed unevaluated Fixture output"
                    raise ValueError(msg)
            file_slots = [item.slot for item in attempt.accepted_files]
            output_slots = list(EXPECTED_STAGE_OUTPUTS[stage])
            if attempt.execution_conclusion == "succeeded":
                if file_slots not in (output_slots, [*output_slots, "diagnostic"]):
                    msg = f"{stage} attempt files do not match the reviewed profile"
                    raise ValueError(msg)
            elif any(item.role == "domain_output" for item in attempt.accepted_files):
                msg = f"{stage} failed attempt cannot contribute Domain output"
                raise ValueError(msg)
            for accepted in attempt.accepted_files:
                if accepted.role == "domain_output":
                    expected_path, expected_media_type, expected_schema = (
                        EXPECTED_STAGE_OUTPUTS[stage][accepted.slot]
                    )
                    if (
                        accepted.path,
                        accepted.media_type,
                        accepted.schema_version,
                    ) != (expected_path, expected_media_type, expected_schema):
                        msg = f"{stage} output does not match the reviewed profile"
                        raise ValueError(msg)
                    sources[accepted.slot] = (
                        archive_path(stage, accepted.path),
                        accepted.schema_version,
                        accepted.byte_size,
                        accepted.sha256,
                        attempt.implementation_mode,
                        attempt.domain_outcome,
                    )
                elif accepted.path != "diagnostic.txt":
                    msg = f"{stage} diagnostic does not match the reviewed profile"
                    raise ValueError(msg)
            if (
                attempt.execution_conclusion != "succeeded"
                or attempt.domain_outcome == "failed"
            ):
                blocking_stage = stage

        expected_artifacts: list[tuple[object, ...]] = []
        for attempt in self.attempts:
            for accepted in attempt.accepted_files:
                common = (
                    accepted.role,
                    attempt.correlation.stage,
                    accepted.slot,
                    archive_path(attempt.correlation.stage, accepted.path),
                    accepted.media_type,
                )
                if accepted.role == "domain_output":
                    expected_artifacts.append(
                        (
                            *common,
                            accepted.schema_version,
                            accepted.byte_size,
                            accepted.sha256,
                        )
                    )
                else:
                    expected_artifacts.append(
                        (
                            *common,
                            accepted.byte_size,
                            accepted.sha256,
                            accepted.truncated,
                        )
                    )
        actual_artifacts: list[tuple[object, ...]] = []
        for artifact in self.artifacts:
            common = (
                artifact.role,
                artifact.stage,
                artifact.slot,
                artifact.path,
                artifact.media_type,
            )
            if artifact.role == "domain_output":
                actual_artifacts.append(
                    (
                        *common,
                        artifact.schema_version,
                        artifact.byte_size,
                        artifact.sha256,
                    )
                )
            else:
                actual_artifacts.append(
                    (
                        *common,
                        artifact.byte_size,
                        artifact.sha256,
                        artifact.truncated,
                    )
                )
        if actual_artifacts != expected_artifacts:
            msg = "artifact inventory does not exactly represent accepted Stage files"
            raise ValueError(msg)
        require_unique_paths(
            [PIPELINE_RESULT_PATH, *(item.path for item in self.artifacts)]
        )
        return self
