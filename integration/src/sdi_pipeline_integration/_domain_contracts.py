"""Authoritative Domain output contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal, Self, get_args

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
    from collections.abc import Sequence

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


FailedImageReason = Literal[
    "unresolved-placement", "no-recipe", "build-failed", "work-limit"
]
FAILED_IMAGE_REASONS: tuple[FailedImageReason, ...] = get_args(FailedImageReason)


class FailedImageRecord(ContractModel):
    """A placement that has no image, with one reason from a closed set."""

    service_id: Slug
    status: Literal["failed"]
    reason: FailedImageReason
    detail: FailureDetail


class NotEvaluatedImageRecord(ContractModel):
    """A Fixture placement without a build claim."""

    service_id: Slug
    status: Literal["not_evaluated"]


ImageRecord = Annotated[
    BuiltImageRecord | FailedImageRecord | NotEvaluatedImageRecord,
    Field(discriminator="status"),
]


REGISTRY = "registry:5000"


def image_tag(*, service_id: str, artifact_id: str, execution_id: str) -> str:
    """Return the tag the image build pushes one placement's image under."""
    return f"{REGISTRY}/sdi/{service_id}:{artifact_id}-{execution_id}"


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


class CountingTopic(ContractModel):
    """Messages counted on one of a service's counting outputs."""

    topic: NonBlank
    messages: NonNegativeInt


class ParticipatingService(ContractModel):
    """A service still running when the orchestrator exited.

    Unless its participation is loosened, it also sent a message on a
    counting output.
    """

    service_id: Slug
    participation: Literal["participated", "participated_loosened"]
    image_digest: ImageDigest
    counting_topics: list[CountingTopic]


DidNotParticipateReason = Literal["container-exited", "failed-to-start", "no-message"]
DID_NOT_PARTICIPATE_REASONS: tuple[DidNotParticipateReason, ...] = get_args(
    DidNotParticipateReason
)


class NonParticipatingService(ContractModel):
    """A service that failed to start, exited, or sent no counted message."""

    service_id: Slug
    participation: Literal["did_not_participate"]
    reason: DidNotParticipateReason
    exit_code: int | SkipJsonSchema[None] = None
    detail: FailureDetail | SkipJsonSchema[None] = None
    image_digest: ImageDigest | SkipJsonSchema[None] = None
    counting_topics: list[CountingTopic] | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require absent facts to be omitted rather than null."""
        return reject_explicit_nulls(
            data, ("exit_code", "detail", "image_digest", "counting_topics")
        )

    @model_validator(mode="after")
    def require_exit_code_of_exited_container(self) -> Self:
        if (self.reason == "container-exited") != (self.exit_code is not None):
            msg = "exit_code is recorded exactly for a container-exited service"
            raise ValueError(msg)
        return self


UnevaluatedReason = Literal[
    "image-unavailable",
    "no-amd64",
    "needs-gpu-or-device",
    "unsupported-command",
    "no-counting-output",
    "unloadable-type",
    "blocked",
]
UNEVALUATED_REASONS: tuple[UnevaluatedReason, ...] = get_args(UnevaluatedReason)


class UnevaluatedService(ContractModel):
    """A service whose participation a pipeline gap kept from being judged."""

    service_id: Slug
    participation: Literal["not_evaluated"]
    reason: UnevaluatedReason
    detail: FailureDetail | SkipJsonSchema[None] = None
    image_digest: ImageDigest | SkipJsonSchema[None] = None
    counting_topics: list[CountingTopic] | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require absent facts to be omitted rather than null."""
        return reject_explicit_nulls(
            data, ("detail", "image_digest", "counting_topics")
        )


ServiceParticipation = Annotated[
    ParticipatingService | NonParticipatingService | UnevaluatedService,
    Field(discriminator="participation"),
]


class MissionStep(ContractModel):
    """One requirement's mission step; waited_on names the topic or action."""

    requirement_id: Slug
    status: Literal["passed", "failed", "timed_out", "scripted", "not_reached"]
    waited_on: NonBlank | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require an unwatched step to omit waited_on rather than null it."""
        return reject_explicit_nulls(data, ("waited_on",))

    @model_validator(mode="after")
    def reject_waiting_scripted_step(self) -> Self:
        if self.status == "scripted" and self.waited_on is not None:
            msg = "a scripted step waits on nothing"
            raise ValueError(msg)
        return self


class Mission(ContractModel):
    """The scenario's setup and one step per requirement, in requirement order."""

    setup: Literal["passed", "timed_out"]
    steps: Annotated[list[MissionStep], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_steps(self) -> Self:
        _require_unique([item.requirement_id for item in self.steps], "requirement_id")
        if self.setup == "timed_out" and any(
            item.status != "not_reached" for item in self.steps
        ):
            msg = "a mission whose setup timed out reaches no step"
            raise ValueError(msg)
        return self

    @property
    def completed(self) -> bool:
        return self.setup == "passed" and all(
            item.status in {"passed", "scripted"} for item in self.steps
        )


def validation_verdict(
    services: Sequence[ServiceParticipation], mission: Mission | None
) -> Literal["succeeded", "failed", "not_evaluated"]:
    if mission is None:
        return "not_evaluated"
    if mission.completed and all(
        isinstance(item, ParticipatingService) for item in services
    ):
        return "succeeded"
    return "failed"


class ValidationEvidence(ContractModel):
    """How CV reached its verdict on one composition in the Validation scenario.

    The verdict is the attempt's domain outcome: not evaluated when a pipeline
    gap kept any service from being judged, succeeded when every service
    participated and the mission completed, and failed otherwise.
    """

    schema_version: Literal["sdi.validation-evidence/v1"]
    evidence_basis: Literal["implemented"]
    validation_evidence_id: Slug
    blueprint_id: Slug
    deployment_schema_id: Slug
    image_build_result_id: Slug
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    combination_id: CombinationId
    profile_id: Slug
    source_inputs: Annotated[list[SourceInputDigest], ProducerInputs("cv")]
    domain_outcome: Literal["succeeded", "failed", "not_evaluated"]
    kpi_evaluation: Literal["not_evaluated"]
    negative_cases: Literal["not_evaluated"]
    platform: Literal["linux/amd64"]
    services: Annotated[list[ServiceParticipation], Field(min_length=1)]
    mission: Mission | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require an absent mission to be omitted rather than null."""
        return reject_explicit_nulls(data, ("mission",))

    @model_validator(mode="after")
    def validate_verdict(self) -> Self:
        _require_unique([item.service_id for item in self.services], "service_id")
        unevaluated = [
            item for item in self.services if isinstance(item, UnevaluatedService)
        ]
        if unevaluated and (
            len(unevaluated) != len(self.services)
            or all(item.reason == "blocked" for item in unevaluated)
        ):
            msg = (
                "a pipeline gap leaves every service not_evaluated, "
                "with at least one cause besides blocked"
            )
            raise ValueError(msg)
        if bool(unevaluated) == (self.mission is not None):
            msg = "evidence holds a mission exactly when the services were evaluated"
            raise ValueError(msg)
        verdict = validation_verdict(self.services, self.mission)
        if self.domain_outcome != verdict:
            msg = f"services and mission make the domain outcome {verdict}"
            raise ValueError(msg)
        return self


KubernetesImageId = Annotated[
    str,
    StringConstraints(
        strict=True, pattern=r"^(?:[\x21-\x3f\x41-\x7e]{1,255}@)?sha256:[0-9a-f]{64}$"
    ),
]
PinnedImage = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^registry:5000/sdi/[a-z][a-z0-9]*(?:-[a-z0-9]+)*@sha256:[0-9a-f]{64}$",
    ),
]
PodPhase = Literal["Pending", "Running", "Succeeded", "Failed", "Unknown"]
DeploymentGap = Literal[
    "unresolved-placement",
    "image-not-built",
    "no-image-for-architecture",
    "host-not-in-cluster",
    "blocked",
]
DEPLOYMENT_GAPS: tuple[DeploymentGap, ...] = get_args(DeploymentGap)
K8sReason = Literal[
    "CrashLoopBackOff", "ErrImagePull", "ImagePullBackOff", "Unschedulable", "other"
]
K8S_REASONS: tuple[K8sReason, ...] = get_args(K8sReason)


def image_digest(image: str) -> str:
    return image.rpartition("@")[2]


def _require_pinned_to_service(service_id: str, image: str | None) -> None:
    if image is not None and not image.startswith(f"registry:5000/sdi/{service_id}@"):
        msg = "a placement image must be registry:5000/sdi/<service_id>@<digest>"
        raise ValueError(msg)


class DeployedPlacement(ContractModel):
    """A placement whose pod ran its pinned image on its host's node and held."""

    service_id: Slug
    host: Slug
    status: Literal["deployed"]
    node: Slug
    image: PinnedImage
    image_id: KubernetesImageId
    phase: Literal["Running"]
    restart_count: Literal[0]

    @model_validator(mode="after")
    def validate_image(self) -> Self:
        _require_pinned_to_service(self.service_id, self.image)
        return self


class FailedPlacement(ContractModel):
    """A placement whose pod broke the pass rule, with Kubernetes' own reason."""

    service_id: Slug
    host: Slug
    status: Literal["failed"]
    detail: FailureDetail
    node: Slug | SkipJsonSchema[None] = None
    image: PinnedImage | SkipJsonSchema[None] = None
    image_id: KubernetesImageId | SkipJsonSchema[None] = None
    phase: PodPhase | SkipJsonSchema[None] = None
    restart_count: NonNegativeInt | SkipJsonSchema[None] = None
    k8s_reason: K8sReason | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        return reject_explicit_nulls(
            data, ("node", "image", "image_id", "phase", "restart_count", "k8s_reason")
        )

    @model_validator(mode="after")
    def validate_image(self) -> Self:
        _require_pinned_to_service(self.service_id, self.image)
        return self


class UndeployedPlacement(ContractModel):
    """A placement that a pipeline gap kept off the cluster."""

    service_id: Slug
    host: Slug | SkipJsonSchema[None] = None
    status: Literal["not_evaluated"]
    reason: DeploymentGap

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require an unresolved host to be omitted rather than null."""
        return reject_explicit_nulls(data, ("host",))


PlacementRecord = Annotated[
    DeployedPlacement | FailedPlacement | UndeployedPlacement,
    Field(discriminator="status"),
]


def deployment_outcome(
    placements: Sequence[PlacementRecord],
) -> Literal["succeeded", "failed", "not_evaluated"]:
    statuses = {item.status for item in placements}
    if "not_evaluated" in statuses:
        return "not_evaluated"
    if "failed" in statuses:
        return "failed"
    return "succeeded"


class DeploymentResult(ContractModel):
    """How CD deployed one composition to the stand-in cluster.

    The outcome is the attempt's domain outcome: not evaluated when a pipeline
    gap kept every placement off the cluster, failed when any placement broke
    the pass rule, and succeeded otherwise. The manifest is the Kubernetes
    v1 List CD applied, recorded whenever it reached the cluster.
    """

    schema_version: Literal["sdi.deployment-result/v1"]
    evidence_basis: Literal["implemented"]
    deployment_result_id: Slug
    blueprint_id: Slug
    deployment_schema_id: Slug
    image_build_result_id: Slug
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    combination_id: CombinationId
    profile_id: Slug
    source_inputs: Annotated[list[SourceInputDigest], ProducerInputs("cd")]
    domain_outcome: Literal["succeeded", "failed", "not_evaluated"]
    cluster_kind: Literal["stand_in"]
    placements: Annotated[list[PlacementRecord], Field(min_length=1)]
    manifest: dict[str, JsonValue] | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        return reject_explicit_nulls(data, ("manifest",))

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        _require_unique([item.service_id for item in self.placements], "service_id")
        undeployed = [
            item for item in self.placements if isinstance(item, UndeployedPlacement)
        ]
        if undeployed and (
            len(undeployed) != len(self.placements)
            or all(item.reason == "blocked" for item in undeployed)
        ):
            msg = (
                "a pipeline gap leaves every placement not_evaluated, "
                "with at least one cause besides blocked"
            )
            raise ValueError(msg)
        if bool(undeployed) == (self.manifest is not None):
            msg = "a result holds a manifest exactly when it was evaluated"
            raise ValueError(msg)
        outcome = deployment_outcome(self.placements)
        if self.domain_outcome != outcome:
            msg = f"placements make the domain outcome {outcome}"
            raise ValueError(msg)
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
