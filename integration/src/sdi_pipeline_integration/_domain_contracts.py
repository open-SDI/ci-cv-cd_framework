"""Authoritative Domain output contracts."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from ._contracts import (
    CombinationId,
    ContractModel,
    NonBlank,
    ScenarioId,
    Slug,
    TestcaseId,
)
from ._stage_contracts import ImageReference, Sha256, SlotName  # noqa: TC001

COMPOSITION_BLUEPRINT_SCHEMA_VERSION = "sdi.composition-blueprint/v1"
DEPLOYMENT_SCHEMA_VERSION = "sdi.deployment-schema/v1"
IMAGE_BUILD_RESULT_SCHEMA_VERSION = "sdi.image-build-result/v1"
VALIDATION_EVIDENCE_SCHEMA_VERSION = "sdi.validation-evidence/v1"
DEPLOYMENT_RESULT_SCHEMA_VERSION = "sdi.deployment-result/v1"
ServiceVersion = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=63),
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
    """One visibly Fixture-owned service in a composition blueprint."""

    service_id: Slug
    version: ServiceVersion
    fixture_source: Slug
    capability: Slug
    depends_on: list[Slug]


class CompositionBlueprint(ContractModel):
    """Target-aware logical service composition without validation claims."""

    schema_version: Literal["sdi.composition-blueprint/v1"]
    evidence_basis: Literal["fixture", "implemented"]
    blueprint_id: Slug
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    combination_id: CombinationId
    profile_id: Slug
    source_inputs: Annotated[list[SourceInputDigest], Field(min_length=3, max_length=3)]
    requirement_ids: Annotated[list[Slug], Field(min_length=1)]
    services: Annotated[list[BlueprintService], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        _require_unique([item.slot for item in self.source_inputs], "source input slot")
        _require_unique(self.requirement_ids, "requirement_id")
        service_ids = [item.service_id for item in self.services]
        _require_unique(service_ids, "service_id")
        service_set = set(service_ids)
        dependencies: dict[str, set[str]] = {}
        for service in self.services:
            _require_unique(service.depends_on, "depends_on")
            if service.service_id in service.depends_on:
                msg = f"service {service.service_id} depends on itself"
                raise ValueError(msg)
            unknown = set(service.depends_on) - service_set
            if unknown:
                msg = f"unknown service dependencies: {sorted(unknown)}"
                raise ValueError(msg)
            dependencies[service.service_id] = set(service.depends_on)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(service_id: str) -> None:
            if service_id in visiting:
                msg = "service dependency graph must be acyclic"
                raise ValueError(msg)
            if service_id in visited:
                return
            visiting.add(service_id)
            for dependency in dependencies[service_id]:
                visit(dependency)
            visiting.remove(service_id)
            visited.add(service_id)

        for service_id in service_ids:
            visit(service_id)
        return self


class DeploymentLocation(ContractModel):
    """One intended mobility or SDI execution location."""

    location_id: Slug
    tier: Literal["mobility", "edge", "fog", "cloud"]
    resource_id: Slug


class ServicePlacement(ContractModel):
    """One service-to-location mapping."""

    service_id: Slug
    location_id: Slug


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
    source_inputs: Annotated[list[SourceInputDigest], Field(min_length=3, max_length=3)]
    locations: Annotated[list[DeploymentLocation], Field(min_length=1)]
    placements: Annotated[list[ServicePlacement], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        _require_unique([item.slot for item in self.source_inputs], "source input slot")
        location_ids = [item.location_id for item in self.locations]
        _require_unique(location_ids, "location_id")
        placement_services = [item.service_id for item in self.placements]
        _require_unique(placement_services, "placed service_id")
        unknown = {item.location_id for item in self.placements} - set(location_ids)
        if unknown:
            msg = f"unknown placement locations: {sorted(unknown)}"
            raise ValueError(msg)
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
    location_id: Slug
    application_status: Literal["not_evaluated"]


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
