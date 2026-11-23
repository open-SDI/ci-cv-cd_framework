"""Per-Stage Domain document checks shared by attempt acceptance and bundles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, assert_never

from ._contracts import TargetExecutionProfile
from ._domain_contracts import (
    BuiltImageRecord,
    CompositionBlueprint,
    CompositionEvidence,
    DeployedPlacement,
    DeploymentResult,
    DeploymentSchema,
    FailedImageRecord,
    ImageBuildResult,
    UndeployedPlacement,
    ValidationEvidence,
    image_digest,
)
from ._stage_registry import STAGES, StageName
from ._yaml_input import InputError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from pydantic import BaseModel

    from ._contracts import Host
    from ._stage_contracts import DomainOutcome, ImplementationMode


@dataclass(frozen=True)
class StageDocuments:
    stage: StageName
    domain_outcome: DomainOutcome
    outputs: Mapping[str, BaseModel]
    inputs: Mapping[str, BaseModel]
    evidence: Sequence[BaseModel]


type StageCheck = Callable[[StageDocuments], None]


def _check_image_records(
    image_build: ImageBuildResult,
    deployment: DeploymentSchema,
    architectures: Mapping[str, str],
) -> None:
    """Check each image record against its placement and host architecture."""
    placements = {item.service_id: item for item in deployment.placements}
    for item in image_build.images:
        unresolved = placements[item.service_id].status == "unresolved"
        if isinstance(item, BuiltImageRecord) and unresolved:
            msg = "image-build result records a built image for an unresolved placement"
            raise InputError(msg)
        if isinstance(item, FailedImageRecord) and unresolved != (
            item.reason == "unresolved-placement"
        ):
            msg = (
                "image-build result must fail exactly the unresolved placements as such"
            )
            raise InputError(msg)
        if (
            isinstance(item, BuiltImageRecord)
            and item.service_id in architectures
            and architectures[item.service_id]
            not in {platform.architecture for platform in item.platforms}
        ):
            msg = "image-build result does not match the accepted target architecture"
            raise InputError(msg)


def _composition_documents(
    documents: StageDocuments,
) -> tuple[CompositionBlueprint, DeploymentSchema, dict[str, Host]]:
    stage, outputs, inputs = documents.stage, documents.outputs, documents.inputs
    blueprint = outputs.get("composition_blueprint") or inputs.get(
        "composition_blueprint"
    )
    deployment = outputs.get("deployment_schema") or inputs.get("deployment_schema")
    if not isinstance(blueprint, CompositionBlueprint) or not isinstance(
        deployment, DeploymentSchema
    ):
        msg = f"{stage} candidate is missing its composition inputs"
        raise InputError(msg)
    if deployment.blueprint_id != blueprint.blueprint_id:
        msg = "deployment schema does not reference the accepted blueprint"
        raise InputError(msg)
    if {item.service_id for item in deployment.placements} != {
        item.service_id for item in blueprint.services
    }:
        msg = "deployment schema must place every accepted blueprint service once"
        raise InputError(msg)
    profile = inputs.get("target_profile")
    profile_hosts = (
        profile.hosts if isinstance(profile, TargetExecutionProfile) else None
    )
    if profile_hosts is not None and any(
        item.host is not None and item.host not in profile_hosts
        for item in deployment.placements
    ):
        msg = "deployment schema names a host outside the target profile"
        raise InputError(msg)
    hosts = {
        item.service_id: profile_hosts[item.host]
        for item in deployment.placements
        if profile_hosts is not None and item.host is not None
    }
    return blueprint, deployment, hosts


def _image_build_documents(
    documents: StageDocuments,
) -> tuple[CompositionBlueprint, DeploymentSchema, ImageBuildResult, dict[str, Host]]:
    blueprint, deployment, hosts = _composition_documents(documents)
    image_build = documents.outputs.get("image_build_result") or documents.inputs.get(
        "image_build_result"
    )
    if not isinstance(image_build, ImageBuildResult):
        msg = f"{documents.stage} candidate is missing the image-build result"
        raise InputError(msg)
    if (
        image_build.blueprint_id != blueprint.blueprint_id
        or image_build.deployment_schema_id != deployment.deployment_schema_id
    ):
        msg = "image-build result does not reference accepted composition outputs"
        raise InputError(msg)
    if {item.service_id for item in image_build.images} != {
        item.service_id for item in blueprint.services
    }:
        msg = "image-build result does not cover the accepted services"
        raise InputError(msg)
    _check_image_records(
        image_build,
        deployment,
        {service_id: host.architecture for service_id, host in hosts.items()},
    )
    return blueprint, deployment, image_build, hosts


def _check_composition(documents: StageDocuments) -> None:
    if documents.domain_outcome == "failed":
        return
    _composition_documents(documents)
    if set(documents.outputs) != {"composition_blueprint", "deployment_schema"}:
        msg = "composition candidate must contain both Domain outputs"
        raise InputError(msg)


def _check_image_build(documents: StageDocuments) -> None:
    if documents.domain_outcome == "failed":
        return
    _image_build_documents(documents)
    if set(documents.outputs) != {"image_build_result"}:
        msg = "image-build candidate must contain its result"
        raise InputError(msg)


def _check_cv(documents: StageDocuments) -> None:
    validation = next(
        (item for item in documents.evidence if isinstance(item, ValidationEvidence)),
        None,
    )
    if validation is None:
        return
    blueprint, deployment, image_build, _ = _image_build_documents(documents)
    if validation.domain_outcome != documents.domain_outcome:
        msg = "Validation evidence outcome does not match the attempt outcome"
        raise InputError(msg)
    if (
        validation.blueprint_id != blueprint.blueprint_id
        or validation.deployment_schema_id != deployment.deployment_schema_id
        or validation.image_build_result_id != image_build.image_build_result_id
    ):
        msg = "Validation evidence does not reference accepted prior outputs"
        raise InputError(msg)
    if {item.service_id for item in validation.services} != {
        item.service_id for item in blueprint.services
    }:
        msg = "Validation evidence does not cover the accepted services"
        raise InputError(msg)
    if (
        validation.mission is not None
        and [item.requirement_id for item in validation.mission.steps]
        != blueprint.requirement_ids
    ):
        msg = "Validation evidence mission does not follow the accepted requirements"
        raise InputError(msg)
    amd64_digests = {
        record.service_id: platform.digest
        for record in image_build.images
        if isinstance(record, BuiltImageRecord)
        for platform in record.platforms
        if platform.architecture == "amd64"
    }
    if any(
        item.image_digest is not None
        and item.image_digest != amd64_digests.get(item.service_id)
        for item in validation.services
    ):
        msg = "Validation evidence names an image other than the built amd64 image"
        raise InputError(msg)


def _check_cd(documents: StageDocuments) -> None:
    result = next(
        (item for item in documents.evidence if isinstance(item, DeploymentResult)),
        None,
    )
    if result is None:
        return
    blueprint, deployment, image_build, hosts = _image_build_documents(documents)
    if result.domain_outcome != documents.domain_outcome:
        msg = "deployment result outcome does not match the attempt outcome"
        raise InputError(msg)
    if (
        result.blueprint_id != blueprint.blueprint_id
        or result.deployment_schema_id != deployment.deployment_schema_id
        or result.image_build_result_id != image_build.image_build_result_id
    ):
        msg = "deployment result does not reference accepted prior outputs"
        raise InputError(msg)
    if {(item.service_id, item.host) for item in result.placements} != {
        (item.service_id, item.host) for item in deployment.placements
    }:
        msg = "deployment result does not cover every intended placement"
        raise InputError(msg)
    platform_digests = {
        (record.service_id, platform.architecture): platform.digest
        for record in image_build.images
        if isinstance(record, BuiltImageRecord)
        for platform in record.platforms
    }
    for item in result.placements:
        host = hosts.get(item.service_id)
        if isinstance(item, UndeployedPlacement) or host is None:
            continue
        built = platform_digests.get((item.service_id, host.architecture))
        images = (
            (item.image, item.image_id)
            if isinstance(item, DeployedPlacement)
            else (item.image,)
        )
        if any(image is not None and image_digest(image) != built for image in images):
            msg = (
                "deployment result names an image other than the one built "
                "for its host's architecture"
            )
            raise InputError(msg)


def _stage_check(stage: StageName) -> StageCheck:
    match stage:
        case "composition":
            return _check_composition
        case "image_build":
            return _check_image_build
        case "cv":
            return _check_cv
        case "cd":
            return _check_cd
        case _:
            assert_never(stage)


CHECKS: dict[StageName, StageCheck] = {stage: _stage_check(stage) for stage in STAGES}


def validate_stage_documents(  # noqa: PLR0913
    stage: StageName,
    outputs: Mapping[str, BaseModel],
    inputs: Mapping[str, BaseModel],
    expected_input_digests: Mapping[str, str],
    expected_identity: tuple[str, str, str, str],
    *,
    implementation_mode: ImplementationMode,
    domain_outcome: DomainOutcome,
    evidence: Sequence[BaseModel] = (),
) -> None:
    """Check a Stage's outputs against each other and the accepted inputs.

    Stage evidence shares the outputs' correlation but is never a Domain output.
    """
    for output in [*outputs.values(), *evidence]:
        if not isinstance(
            output,
            (
                CompositionBlueprint,
                DeploymentSchema,
                ImageBuildResult,
                ValidationEvidence,
                DeploymentResult,
                CompositionEvidence,
            ),
        ):
            msg = "candidate output is not a supported Domain contract"
            raise InputError(msg)
        actual_identity = (
            output.scenario_id,
            output.testcase_id,
            output.combination_id,
            output.profile_id,
        )
        if actual_identity != expected_identity:
            msg = "candidate Domain output correlation does not match accepted inputs"
            raise InputError(msg)
        if {
            item.slot: item.sha256 for item in output.source_inputs
        } != expected_input_digests:
            msg = "candidate Domain output input digests do not match the request"
            raise InputError(msg)
    published = {getattr(item, "schema_version", None) for item in evidence}
    if implementation_mode == "implemented" and any(
        grant.required and grant.schema_version not in published
        for grant in STAGES[stage].grant.evidence
    ):
        msg = f"implemented {stage} candidate did not produce its required output"
        raise InputError(msg)
    if domain_outcome == "failed" and outputs:
        msg = f"{stage} negative Domain outcome cannot carry Domain output"
        raise InputError(msg)
    CHECKS[stage](StageDocuments(stage, domain_outcome, outputs, inputs, evidence))
