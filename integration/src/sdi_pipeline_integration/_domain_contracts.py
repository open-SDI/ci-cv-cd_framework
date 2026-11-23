"""Authoritative Domain output contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal, Self

from pydantic import (
    Field,
    JsonValue,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema  # noqa: TC002
from pydantic_core import core_schema

from ._contracts import (
    CombinationId,
    ContractModel,
    NonBlank,
    ScenarioId,
    Slug,
    TestcaseId,
    reject_explicit_nulls,
)
from ._stage_contracts import Sha256, SlotName  # noqa: TC001
from ._stage_registry import EXPECTED_STAGE_INPUT_SLOTS, StageName

if TYPE_CHECKING:
    from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
    from pydantic.json_schema import JsonSchemaValue

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


@dataclass(frozen=True)
class ProducerInputs:
    """Require source inputs to name every input of the producing Stage, in order."""

    stage: StageName

    def __get_pydantic_core_schema__(
        self, source: object, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            self._validate, handler(source)
        )

    def __get_pydantic_json_schema__(
        self, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        json_schema = handler(schema)
        slots = EXPECTED_STAGE_INPUT_SLOTS[self.stage]
        json_schema["minItems"] = json_schema["maxItems"] = len(slots)
        json_schema["prefixItems"] = [
            {"allOf": [json_schema["items"]], "properties": {"slot": {"const": slot}}}
            for slot in slots
        ]
        return json_schema

    def _validate(self, value: list[SourceInputDigest]) -> list[SourceInputDigest]:
        if tuple(item.slot for item in value) != EXPECTED_STAGE_INPUT_SLOTS[self.stage]:
            msg = f"source inputs must name each {self.stage} input slot once, in order"
            raise ValueError(msg)
        return value


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
    profile_basis: Literal["declared", "placeholder"]
    source_inputs: Annotated[list[SourceInputDigest], ProducerInputs("composition")]
    requirement_ids: Annotated[list[Slug], Field(min_length=1)]
    services: Annotated[list[BlueprintService], Field(min_length=1)]
    coverage: Annotated[list[BlueprintCoverage], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_services(self) -> Self:
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
    profile_basis: Literal["declared", "placeholder"]
    source_inputs: Annotated[list[SourceInputDigest], ProducerInputs("composition")]
    placements: Annotated[list[ServicePlacement], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        _require_unique(
            [item.service_id for item in self.placements], "placed service_id"
        )
        return self


ImageDigest = Annotated[
    str, StringConstraints(strict=True, pattern=r"^sha256:[0-9a-f]{64}$")
]
# The redacted last error line: printable ASCII without quotes or backslashes,
# so a summary can show it without escaping.
FailureDetail = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[\x20\x21\x23-\x5b\x5d-\x7e]{1,256}$"),
]


class PlatformImage(ContractModel):
    """The image built for one architecture."""

    architecture: Literal["amd64", "arm64"]
    digest: ImageDigest


class BuiltImageRecord(ContractModel):
    """A placement whose image was pushed to the pipeline registry.

    The digest is an index for several architectures and a plain manifest for
    one.
    """

    service_id: Slug
    status: Literal["built"]
    repository: Annotated[
        str,
        StringConstraints(
            strict=True, pattern=r"^sdi/[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=67
        ),
    ]
    digest: ImageDigest
    platforms: Annotated[list[PlatformImage], Field(min_length=1, max_length=2)]

    @model_validator(mode="after")
    def validate_image(self) -> Self:
        if self.repository != f"sdi/{self.service_id}":
            msg = "a built image repository must be sdi/<service_id>"
            raise ValueError(msg)
        _require_unique(
            [item.architecture for item in self.platforms], "platform architecture"
        )
        return self


class FailedImageRecord(ContractModel):
    """A placement that has no image, with one reason from a closed set."""

    service_id: Slug
    status: Literal["failed"]
    reason: Literal["unresolved-placement", "no-recipe", "build-failed", "work-limit"]
    detail: FailureDetail


class NotEvaluatedImageRecord(ContractModel):
    """A Fixture placement without a build claim."""

    service_id: Slug
    status: Literal["not_evaluated"]


ImageRecord = Annotated[
    BuiltImageRecord | FailedImageRecord | NotEvaluatedImageRecord,
    Field(discriminator="status"),
]


class ImageBuildResult(ContractModel):
    """One image-build record per placement of one composition."""

    schema_version: Literal["sdi.image-build-result/v1"]
    evidence_basis: Literal["fixture", "implemented"]
    image_build_result_id: Slug
    blueprint_id: Slug
    deployment_schema_id: Slug
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    combination_id: CombinationId
    profile_id: Slug
    source_inputs: Annotated[list[SourceInputDigest], ProducerInputs("image_build")]
    # Stack-local: nothing outside the Jenkins stack pulls from it.
    registry: Literal["registry:5000"]
    images: Annotated[list[ImageRecord], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_records(self) -> Self:
        _require_unique([item.service_id for item in self.images], "image service_id")
        if any(
            (item.status == "not_evaluated") != (self.evidence_basis == "fixture")
            for item in self.images
        ):
            msg = (
                "a fixture result records every image not_evaluated, "
                "and an implemented result none"
            )
            raise ValueError(msg)
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
    source_inputs: Annotated[list[SourceInputDigest], ProducerInputs("cv")]
    domain_outcome: Literal["not_evaluated"]
    kpi_evaluation: Literal["not_evaluated"]
    validation_records: Annotated[list[FixtureValidationRecord], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_records(self) -> Self:
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
    source_inputs: Annotated[list[SourceInputDigest], ProducerInputs("cd")]
    domain_outcome: Literal["not_evaluated"]
    application_status: Literal["not_evaluated"]
    deployment_records: Annotated[list[FixtureDeploymentRecord], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_records(self) -> Self:
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
    profile_basis: Literal["declared", "placeholder"]
    source_inputs: Annotated[list[SourceInputDigest], ProducerInputs("composition")]
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
        if (self.generation.outcome == "generated") != (self.assessment is not None):
            msg = "evidence holds an assessment exactly when proposals were generated"
            raise ValueError(msg)
        return self
