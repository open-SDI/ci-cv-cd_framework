"""Authoritative Domain output contracts."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    Field,
    JsonValue,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema  # noqa: TC002

from ._contracts import (
    CombinationId,
    ContractModel,
    NonBlank,
    ScenarioId,
    Slug,
    TestcaseId,
    reject_explicit_nulls,
)
from ._stage_contracts import ImageReference, Sha256, SlotName  # noqa: TC001

COMPOSITION_BLUEPRINT_SCHEMA_VERSION = "sdi.composition-blueprint/v1"
DEPLOYMENT_SCHEMA_VERSION = "sdi.deployment-schema/v1"
IMAGE_BUILD_RESULT_SCHEMA_VERSION = "sdi.image-build-result/v1"
VALIDATION_EVIDENCE_SCHEMA_VERSION = "sdi.validation-evidence/v1"
DEPLOYMENT_RESULT_SCHEMA_VERSION = "sdi.deployment-result/v1"
COMPOSITION_EVIDENCE_SCHEMA_VERSION = "sdi.composition-evidence/v1"

# A short label without path or URL syntax, so a model-chosen capability can
# never carry an endpoint or a machine path into a Domain output.
Capability = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[^/:]+$"
    ),
]
type CoverageStatus = Literal[
    "supported", "missing", "uncertain", "outside_composition"
]


def _require_unique(values: list[str], field_name: str) -> None:
    if len(values) != len(set(values)):
        msg = f"{field_name} values must be unique"
        raise ValueError(msg)


class SourceInputDigest(ContractModel):
    """Stable exact-byte input correlation embedded in Domain outputs."""

    slot: SlotName
    sha256: Sha256


class BlueprintService(ContractModel):
    """One selected service; capability is an unchecked assigned label.

    basis carries the description's provenance, so a placeholder stays visible.
    """

    service_id: Slug
    basis: Literal["declared", "placeholder"]
    capability: Capability
    depends_on: list[Slug]


class BlueprintCoverage(ContractModel):
    """The checked coverage status of one requirement obligation."""

    requirement_id: Slug
    status: CoverageStatus
    services: list[Slug]


class CompositionBlueprint(ContractModel):
    """Target-aware logical service composition without validation claims."""

    schema_version: Literal["sdi.composition-blueprint/v1"]
    evidence_basis: Literal["fixture", "implemented"]
    blueprint_id: Slug
    proposal_id: Slug
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    combination_id: CombinationId
    profile_id: Slug
    source_inputs: Annotated[list[SourceInputDigest], Field(min_length=5, max_length=5)]
    requirement_ids: Annotated[list[Slug], Field(min_length=1)]
    services: Annotated[list[BlueprintService], Field(min_length=1)]
    coverage: Annotated[list[BlueprintCoverage], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_services(self) -> Self:
        _require_unique([item.slot for item in self.source_inputs], "source input slot")
        _require_unique(self.requirement_ids, "requirement_id")
        _require_unique([item.service_id for item in self.services], "service_id")
        for service in self.services:
            _require_unique(service.depends_on, "depends_on")
        if [item.requirement_id for item in self.coverage] != self.requirement_ids:
            msg = "coverage must list each requirement_id once, in order"
            raise ValueError(msg)
        service_ids = {item.service_id for item in self.services}
        for entry in self.coverage:
            _require_unique(entry.services, "covering service_id")
            if not set(entry.services) <= service_ids:
                msg = "coverage cites a service outside the blueprint"
                raise ValueError(msg)
        return self


class ServicePlacement(ContractModel):
    """One service placed as a chosen artifact on a profile host."""

    service_id: Slug
    status: Literal["resolved", "unresolved"]
    artifact_id: Slug | SkipJsonSchema[None] = None
    host: Slug | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require an unresolved artifact or host to be omitted rather than null."""
        return reject_explicit_nulls(data, ("artifact_id", "host"))

    @model_validator(mode="after")
    def require_resolved_facts(self) -> Self:
        if self.status == "resolved" and (
            self.artifact_id is None or self.host is None
        ):
            msg = "a resolved placement requires its artifact_id and host"
            raise ValueError(msg)
        return self


class DeploymentSchema(ContractModel):
    """Intended placement for every service in one accepted blueprint."""

    schema_version: Literal["sdi.deployment-schema/v1"]
    evidence_basis: Literal["fixture", "implemented"]
    deployment_schema_id: Slug
    blueprint_id: Slug
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    combination_id: CombinationId
    profile_id: Slug
    source_inputs: Annotated[list[SourceInputDigest], Field(min_length=5, max_length=5)]
    placements: Annotated[list[ServicePlacement], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        _require_unique([item.slot for item in self.source_inputs], "source input slot")
        _require_unique(
            [item.service_id for item in self.placements], "placed service_id"
        )
        return self


class FixtureImageRecord(ContractModel):
    """One deterministic image reference without a build-success claim."""

    service_id: Slug
    architecture: Literal["arm64", "amd64"]
    image: ImageReference
    build_status: Literal["not_evaluated"]


class ImageBuildResult(ContractModel):
    """Fixture-qualified image-build handoff for one composition."""

    schema_version: Literal["sdi.image-build-result/v1"]
    evidence_basis: Literal["fixture"]
    image_build_result_id: Slug
    blueprint_id: Slug
    deployment_schema_id: Slug
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    combination_id: CombinationId
    profile_id: Slug
    source_inputs: Annotated[list[SourceInputDigest], Field(min_length=3, max_length=3)]
    images: Annotated[list[FixtureImageRecord], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_records(self) -> Self:
        _require_unique([item.slot for item in self.source_inputs], "source input slot")
        _require_unique([item.service_id for item in self.images], "image service_id")
        return self


class FixtureValidationRecord(ContractModel):
    """One declared validation case that was deliberately not evaluated."""

    validation_case_id: Slug
    description: NonBlank
    evaluation_status: Literal["not_evaluated"]


class ValidationEvidence(ContractModel):
    """Fixture-qualified CV handoff that contains no Validation verdict."""

    schema_version: Literal["sdi.validation-evidence/v1"]
    evidence_basis: Literal["fixture"]
    validation_evidence_id: Slug
    blueprint_id: Slug
    deployment_schema_id: Slug
    image_build_result_id: Slug
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    combination_id: CombinationId
    profile_id: Slug
    source_inputs: Annotated[list[SourceInputDigest], Field(min_length=5, max_length=5)]
    domain_outcome: Literal["not_evaluated"]
    kpi_evaluation: Literal["not_evaluated"]
    validation_records: Annotated[list[FixtureValidationRecord], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_records(self) -> Self:
        _require_unique([item.slot for item in self.source_inputs], "source input slot")
        _require_unique(
            [item.validation_case_id for item in self.validation_records],
            "validation_case_id",
        )
        return self


class FixtureDeploymentRecord(ContractModel):
    """One intended placement that was deliberately not applied."""

    service_id: Slug
    host: Slug | SkipJsonSchema[None] = None
    application_status: Literal["not_evaluated"]

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require an unresolved host to be omitted rather than null."""
        return reject_explicit_nulls(data, ("host",))


class DeploymentResult(ContractModel):
    """Fixture-qualified CD handoff that contains no deployment claim."""

    schema_version: Literal["sdi.deployment-result/v1"]
    evidence_basis: Literal["fixture"]
    deployment_result_id: Slug
    blueprint_id: Slug
    deployment_schema_id: Slug
    image_build_result_id: Slug
    validation_evidence_id: Slug
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    combination_id: CombinationId
    profile_id: Slug
    source_inputs: Annotated[list[SourceInputDigest], Field(min_length=5, max_length=5)]
    domain_outcome: Literal["not_evaluated"]
    application_status: Literal["not_evaluated"]
    deployment_records: Annotated[list[FixtureDeploymentRecord], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_records(self) -> Self:
        _require_unique([item.slot for item in self.source_inputs], "source input slot")
        _require_unique(
            [item.service_id for item in self.deployment_records],
            "deployment service_id",
        )
        return self


class EvidenceDescription(ContractModel):
    """One repository service description; discovery records which it supplied."""

    path: NonBlank
    sha256: Sha256


class GenerationRecord(ContractModel):
    """The selected generator's settings, exact request and response summary."""

    composition_config_sha256: Sha256
    generator: Slug
    model: NonBlank
    reasoning_effort: NonBlank
    max_output_tokens: PositiveInt
    timeout_seconds: PositiveInt
    max_context_bytes: PositiveInt
    base_url: NonBlank
    max_retries: NonNegativeInt
    outcome: Literal["generated", "exhausted"]
    request: dict[str, JsonValue]
    response: dict[str, JsonValue]


class CompositionEvidence(ContractModel):
    """How the composition Stage reached its outcome; never a Domain input."""

    schema_version: Literal["sdi.composition-evidence/v1"]
    evidence_basis: Literal["implemented"]
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    combination_id: CombinationId
    profile_id: Slug
    source_inputs: Annotated[list[SourceInputDigest], Field(min_length=5, max_length=5)]
    descriptions: Annotated[list[EvidenceDescription], Field(min_length=1)]
    generation: GenerationRecord
    discovery: dict[str, JsonValue]
    assessment: dict[str, JsonValue] | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require an absent assessment to be omitted rather than null."""
        return reject_explicit_nulls(data, ("assessment",))

    @model_validator(mode="after")
    def require_assessment_of_generated_proposals(self) -> Self:
        _require_unique([item.slot for item in self.source_inputs], "source input slot")
        if (self.generation.outcome == "generated") != (self.assessment is not None):
            msg = "evidence holds an assessment exactly when proposals were generated"
            raise ValueError(msg)
        return self
