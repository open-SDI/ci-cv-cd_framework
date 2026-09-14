"""Authoritative v1 input contracts for pipeline integration runs."""

from __future__ import annotations

from typing import Annotated, Literal, Self, cast

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    StrictFloat,
    StrictInt,
    StringConstraints,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema  # noqa: TC002

RUN_REQUEST_SCHEMA_VERSION = "sdi.pipeline-integration-run-request/v1"
REQUIREMENTS_SCHEMA_VERSION = "sdi.mobility-requirements-specification/v1"
TARGET_PROFILE_SCHEMA_VERSION = "sdi.target-execution-profile/v1"
REPOSITORY_PATH_SCHEMA_PATTERN = (
    r"^(?!/)(?![A-Za-z]:/)(?!.*(?:^|/)\.{1,2}(?:/|$))(?!.*//)(?!.*\\)"
    r"(?!.*[\x00-\x1f\x7f]).*[^/]$"
)
GITHUB_REPOSITORY_IDENTITY_PATTERN = r"^github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Slug = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$",
        max_length=63,
    ),
]
ScenarioId = Annotated[str, StringConstraints(strict=True, pattern=r"^S-[0-9]{2}$")]
TestcaseId = Annotated[str, StringConstraints(strict=True, pattern=r"^TC-[0-9]{2}$")]
CombinationId = Annotated[str, StringConstraints(strict=True, pattern=r"^C-[0-9]{2}$")]
RepositoryPath = Annotated[
    str,
    StringConstraints(strict=True, min_length=1),
    Field(json_schema_extra={"pattern": REPOSITORY_PATH_SCHEMA_PATTERN}),
]
RepositoryIdentity = Annotated[
    str,
    StringConstraints(strict=True, pattern=GITHUB_REPOSITORY_IDENTITY_PATTERN),
]
type Quantity = StrictInt | StrictFloat
type PositiveQuantity = (
    Annotated[StrictInt, Field(gt=0)] | Annotated[StrictFloat, Field(gt=0)]
)
type NonNegativeQuantity = (
    Annotated[StrictInt, Field(ge=0)] | Annotated[StrictFloat, Field(ge=0)]
)
type PositiveWholeQuantity = Annotated[StrictInt, Field(gt=0)]
type NonNegativeWholeQuantity = Annotated[StrictInt, Field(ge=0)]


class ContractModel(BaseModel):
    """Base for closed, immutable, strict input contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
    )


class PlaceholderValue[ValueT](ContractModel):
    """A quantitative value carrying no capability evidence."""

    value: ValueT
    basis: Literal["placeholder"]
    note: NonBlank


class DeclaredValue[ValueT](ContractModel):
    """A quantitative value asserted by an accountable source."""

    value: ValueT
    basis: Literal["declared"]
    source: NonBlank


class MeasuredValue[ValueT](ContractModel):
    """A quantitative value observed under recorded conditions."""

    value: ValueT
    basis: Literal["measured"]
    method: NonBlank
    measured_at: AwareDatetime
    conditions: NonBlank
    evidence_reference: NonBlank


ValueWithBasis = Annotated[
    PlaceholderValue[Quantity] | DeclaredValue[Quantity] | MeasuredValue[Quantity],
    Field(discriminator="basis"),
]
PositiveValueWithBasis = Annotated[
    PlaceholderValue[PositiveQuantity]
    | DeclaredValue[PositiveQuantity]
    | MeasuredValue[PositiveQuantity],
    Field(discriminator="basis"),
]
NonNegativeValueWithBasis = Annotated[
    PlaceholderValue[NonNegativeQuantity]
    | DeclaredValue[NonNegativeQuantity]
    | MeasuredValue[NonNegativeQuantity],
    Field(discriminator="basis"),
]
PositiveWholeValueWithBasis = Annotated[
    PlaceholderValue[PositiveWholeQuantity]
    | DeclaredValue[PositiveWholeQuantity]
    | MeasuredValue[PositiveWholeQuantity],
    Field(discriminator="basis"),
]
NonNegativeWholeValueWithBasis = Annotated[
    PlaceholderValue[NonNegativeWholeQuantity]
    | DeclaredValue[NonNegativeWholeQuantity]
    | MeasuredValue[NonNegativeWholeQuantity],
    Field(discriminator="basis"),
]


def _reject_explicit_nulls(data: object, field_names: tuple[str, ...]) -> object:
    if isinstance(data, dict):
        values = cast("dict[object, object]", data)
        for field_name in field_names:
            if field_name in values and values[field_name] is None:
                msg = f"{field_name} must be omitted when unknown"
                raise ValueError(msg)
    return cast("object", data)


class RunCombination(ContractModel):
    """The one testcase-local combination selected by a run request."""

    combination_id: CombinationId
    target_profile: RepositoryPath


class RunRequest(ContractModel):
    """One committed pipeline integration run request."""

    schema_version: Literal["sdi.pipeline-integration-run-request/v1"]
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    requirements_specification: RepositoryPath
    combination: RunCombination


class ContextParameter(ContractModel):
    """A named system-context parameter defined once for requirements."""

    parameter_id: Slug
    description: NonBlank
    unit: NonBlank
    value: ValueWithBasis


class SystemContext(ContractModel):
    """Mission context and its named quantitative parameters."""

    description: NonBlank
    parameters: list[ContextParameter]


class Stakeholder(ContractModel):
    """A stakeholder named by the specification."""

    stakeholder_id: Slug
    name: NonBlank


class Definition(ContractModel):
    """A term defined within the specification."""

    term: NonBlank
    definition: NonBlank


class ExternalInterface(ContractModel):
    """An external interface relevant to the mission."""

    interface_id: Slug
    description: NonBlank


class StakeholderNeed(ContractModel):
    """A traceability target for normative requirements."""

    need_id: Slug
    stakeholder_id: Slug
    statement: NonBlank


class Verification(ContractModel):
    """A planned requirement-verification method and criteria."""

    method: Literal["test", "analysis", "inspection", "demonstration"]
    acceptance_criteria: Annotated[list[NonBlank], Field(min_length=1)]


class Requirement(ContractModel):
    """One uniquely identified, traceable mobility requirement."""

    requirement_id: Slug
    type: Literal["functional", "non_functional"]
    title: NonBlank
    normative_statement: NonBlank
    priority: Literal["must", "should", "could"]
    rationale: NonBlank
    source: NonBlank
    traces_to: Annotated[list[Slug], Field(min_length=1)]
    depends_on: list[Slug]
    verification: Verification


def _require_unique(values: list[str], field_name: str) -> None:
    if len(values) != len(set(values)):
        msg = f"{field_name} values must be unique"
        raise ValueError(msg)


class MobilityRequirementsSpecification(ContractModel):
    """A lean, versioned Mobility Requirements Specification."""

    schema_version: Literal["sdi.mobility-requirements-specification/v1"]
    document_version: PositiveInt
    purpose: NonBlank
    scope: NonBlank
    system_context: SystemContext
    stakeholders: Annotated[list[Stakeholder], Field(min_length=1)]
    definitions: list[Definition]
    assumptions: list[NonBlank]
    dependencies: list[NonBlank]
    external_interfaces: list[ExternalInterface]
    stakeholder_needs: Annotated[list[StakeholderNeed], Field(min_length=1)]
    requirements: Annotated[list[Requirement], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_relationships(self) -> Self:  # noqa: C901
        """Require unique identities and closed traceability relationships."""
        parameter_ids = [item.parameter_id for item in self.system_context.parameters]
        stakeholder_ids = [item.stakeholder_id for item in self.stakeholders]
        interface_ids = [item.interface_id for item in self.external_interfaces]
        need_ids = [item.need_id for item in self.stakeholder_needs]
        requirement_ids = [item.requirement_id for item in self.requirements]

        for values, name in (
            (parameter_ids, "parameter_id"),
            (stakeholder_ids, "stakeholder_id"),
            (interface_ids, "interface_id"),
            (need_ids, "need_id"),
            (requirement_ids, "requirement_id"),
        ):
            _require_unique(values, name)

        if set(need_ids) & set(requirement_ids):
            msg = "need_id and requirement_id values must not overlap"
            raise ValueError(msg)

        stakeholder_set = set(stakeholder_ids)
        for need in self.stakeholder_needs:
            if need.stakeholder_id not in stakeholder_set:
                msg = f"unknown stakeholder_id: {need.stakeholder_id}"
                raise ValueError(msg)

        need_set = set(need_ids)
        requirement_set = set(requirement_ids)
        dependencies: dict[str, set[str]] = {}
        traced_needs: set[str] = set()
        for requirement in self.requirements:
            _require_unique(requirement.traces_to, "traces_to")
            _require_unique(requirement.depends_on, "depends_on")
            unknown_needs = set(requirement.traces_to) - need_set
            if unknown_needs:
                msg = f"unknown traces_to values: {sorted(unknown_needs)}"
                raise ValueError(msg)
            unknown_dependencies = set(requirement.depends_on) - requirement_set
            if unknown_dependencies:
                msg = f"unknown depends_on values: {sorted(unknown_dependencies)}"
                raise ValueError(msg)
            if requirement.requirement_id in requirement.depends_on:
                msg = f"requirement {requirement.requirement_id} depends on itself"
                raise ValueError(msg)
            traced_needs.update(requirement.traces_to)
            dependencies[requirement.requirement_id] = set(requirement.depends_on)

        untraced_needs = need_set - traced_needs
        if untraced_needs:
            msg = f"untraced stakeholder needs: {sorted(untraced_needs)}"
            raise ValueError(msg)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(requirement_id: str) -> None:
            if requirement_id in visiting:
                msg = "requirement dependency graph must be acyclic"
                raise ValueError(msg)
            if requirement_id in visited:
                return
            visiting.add(requirement_id)
            for dependency in dependencies[requirement_id]:
                visit(dependency)
            visiting.remove(requirement_id)
            visited.add(requirement_id)

        for requirement_id in requirement_ids:
            visit(requirement_id)
        return self


class MobilityTarget(ContractModel):
    """The mobility target described by a reusable profile."""

    target_id: Slug
    kind: Literal["sdv", "sdr"]


class ComputePlatform(ContractModel):
    """The compute platform and CPU architecture used by a target."""

    platform_id: Slug
    kind: Literal["native", "controller", "edge"]
    architecture: Literal["arm64", "amd64"]


class ResourceCapacity(ContractModel):
    """Optional known target capacity; omitted quantities are unknown."""

    model_config = ConfigDict(json_schema_extra={"minProperties": 1})

    cpu_cores: PositiveValueWithBasis | SkipJsonSchema[None] = None
    memory_mib: PositiveWholeValueWithBasis | SkipJsonSchema[None] = None
    gpu_count: NonNegativeWholeValueWithBasis | SkipJsonSchema[None] = None
    gpu_model: NonBlank | SkipJsonSchema[None] = None
    gpu_vram_mib: PositiveWholeValueWithBasis | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require unknown quantities to be omitted rather than null."""
        return _reject_explicit_nulls(
            data,
            (
                "cpu_cores",
                "memory_mib",
                "gpu_count",
                "gpu_model",
                "gpu_vram_mib",
            ),
        )

    @model_validator(mode="after")
    def require_a_capacity(self) -> Self:
        """Reject a present but empty capacity object."""
        if not self.model_fields_set:
            msg = "resources must contain at least one capacity"
            raise ValueError(msg)
        return self


class NetworkLink(ContractModel):
    """One directional network link under stated conditions."""

    model_config = ConfigDict(
        json_schema_extra={
            "anyOf": [
                {"required": ["latency_ms"]},
                {"required": ["bandwidth_mbps"]},
            ]
        }
    )

    source: Slug
    destination: Slug
    conditions: NonBlank
    latency_ms: NonNegativeValueWithBasis | SkipJsonSchema[None] = None
    bandwidth_mbps: PositiveValueWithBasis | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require unknown network quantities to be omitted rather than null."""
        return _reject_explicit_nulls(data, ("latency_ms", "bandwidth_mbps"))

    @model_validator(mode="after")
    def validate_link(self) -> Self:
        """Require a direction and at least one known quantity."""
        if self.source == self.destination:
            msg = "network link endpoints must differ"
            raise ValueError(msg)
        if self.latency_ms is None and self.bandwidth_mbps is None:
            msg = "network link must contain latency_ms or bandwidth_mbps"
            raise ValueError(msg)
        return self


class TargetExecutionProfile(ContractModel):
    """A reusable description of one candidate execution environment."""

    schema_version: Literal["sdi.target-execution-profile/v1"]
    profile_id: Slug
    target: MobilityTarget
    platform: ComputePlatform
    resources: ResourceCapacity | SkipJsonSchema[None] = None
    network_links: (
        Annotated[list[NetworkLink], Field(min_length=1)] | SkipJsonSchema[None]
    ) = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require absent optional sections to be omitted rather than null."""
        return _reject_explicit_nulls(data, ("resources", "network_links"))

    @model_validator(mode="after")
    def validate_network_links(self) -> Self:
        """Require unique directional links between profile endpoints."""
        links = self.network_links or []
        endpoints = {self.target.target_id, self.platform.platform_id}
        directions: list[tuple[str, str]] = []
        for link in links:
            if {link.source, link.destination} - endpoints:
                msg = "network link endpoints must name the target or platform"
                raise ValueError(msg)
            directions.append((link.source, link.destination))
        if len(directions) != len(set(directions)):
            msg = "network link directions must be unique"
            raise ValueError(msg)
        return self
