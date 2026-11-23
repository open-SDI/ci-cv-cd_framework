"""Per-Stage Domain document checks shared by attempt acceptance and bundles."""

from __future__ import annotations

from typing import TYPE_CHECKING, assert_never

from ._contracts import TargetExecutionProfile
from ._domain_contracts import (
    BuiltImageRecord,
    CompositionBlueprint,
    CompositionEvidence,
    DeploymentResult,
    DeploymentSchema,
    FailedImageRecord,
    ImageBuildResult,
    ValidationEvidence,
)
from ._stage_registry import STAGES, StageName
from ._yaml_input import InputError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from pydantic import BaseModel

    from ._contracts import Host
    from ._stage_contracts import DomainOutcome

type StageCheck = Callable[
    [StageName, Mapping[str, BaseModel], Mapping[str, BaseModel]], None
]


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
    stage: StageName,
    outputs: Mapping[str, BaseModel],
    inputs: Mapping[str, BaseModel],
) -> tuple[CompositionBlueprint, DeploymentSchema, dict[str, Host]]:
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
    stage: StageName,
    outputs: Mapping[str, BaseModel],
    inputs: Mapping[str, BaseModel],
) -> tuple[CompositionBlueprint, DeploymentSchema, ImageBuildResult]:
    blueprint, deployment, hosts = _composition_documents(stage, outputs, inputs)
    image_build = outputs.get("image_build_result") or inputs.get("image_build_result")
    if not isinstance(image_build, ImageBuildResult):
        msg = f"{stage} candidate is missing the image-build result"
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
    return blueprint, deployment, image_build


def _validation_documents(
    stage: StageName,
    outputs: Mapping[str, BaseModel],
    inputs: Mapping[str, BaseModel],
) -> tuple[
    CompositionBlueprint, DeploymentSchema, ImageBuildResult, ValidationEvidence
]:
    blueprint, deployment, image_build = _image_build_documents(stage, outputs, inputs)
    validation = outputs.get("validation_evidence") or inputs.get("validation_evidence")
    if not isinstance(validation, ValidationEvidence):
        msg = f"{stage} candidate is missing Validation evidence"
        raise InputError(msg)
    if (
        validation.blueprint_id != blueprint.blueprint_id
        or validation.deployment_schema_id != deployment.deployment_schema_id
        or validation.image_build_result_id != image_build.image_build_result_id
    ):
        msg = "Validation evidence does not reference accepted prior outputs"
        raise InputError(msg)
    return blueprint, deployment, image_build, validation


def _check_composition(
    stage: StageName,
    outputs: Mapping[str, BaseModel],
    inputs: Mapping[str, BaseModel],
) -> None:
    _composition_documents(stage, outputs, inputs)
    if set(outputs) != {"composition_blueprint", "deployment_schema"}:
        msg = "composition candidate must contain both Domain outputs"
        raise InputError(msg)


def _check_image_build(
    stage: StageName,
    outputs: Mapping[str, BaseModel],
    inputs: Mapping[str, BaseModel],
) -> None:
    _image_build_documents(stage, outputs, inputs)
    if set(outputs) != {"image_build_result"}:
        msg = "image-build candidate must contain its result"
        raise InputError(msg)


def _check_cv(
    stage: StageName,
    outputs: Mapping[str, BaseModel],
    inputs: Mapping[str, BaseModel],
) -> None:
    _validation_documents(stage, outputs, inputs)
    if set(outputs) != {"validation_evidence"}:
        msg = "CV candidate must contain Validation evidence"
        raise InputError(msg)


def _check_cd(
    stage: StageName,
    outputs: Mapping[str, BaseModel],
    inputs: Mapping[str, BaseModel],
) -> None:
    blueprint, deployment, image_build, validation = _validation_documents(
        stage, outputs, inputs
    )
    deployment_result = outputs.get("deployment_result")
    if not isinstance(deployment_result, DeploymentResult):
        msg = "CD candidate must contain its deployment result"
        raise InputError(msg)
    if (
        deployment_result.blueprint_id != blueprint.blueprint_id
        or deployment_result.deployment_schema_id != deployment.deployment_schema_id
        or deployment_result.image_build_result_id != image_build.image_build_result_id
        or deployment_result.validation_evidence_id != validation.validation_evidence_id
    ):
        msg = "deployment result does not reference accepted prior outputs"
        raise InputError(msg)
    if {
        (item.service_id, item.host) for item in deployment_result.deployment_records
    } != {(item.service_id, item.host) for item in deployment.placements}:
        msg = "deployment result does not cover every intended placement"
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
    if domain_outcome == "failed":
        if outputs:
            msg = f"{stage} negative Domain outcome cannot carry Domain output"
            raise InputError(msg)
        return
    CHECKS[stage](stage, outputs, inputs)
