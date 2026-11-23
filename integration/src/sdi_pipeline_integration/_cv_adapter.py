from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from ._adapter_kit import Verdict, encode_document, serve
from ._contracts import TargetExecutionProfile
from ._cv_diagnostic import describe
from ._cv_run import (
    ENGINE_LOST,
    RunRequest,
    SimBuildFailed,
    SimImage,
    prepare_engine,
    run_composition,
)
from ._cv_verdict import Composed, CvError, Judgement, assess
from ._domain_contracts import (
    CompositionBlueprint,
    DeploymentSchema,
    ImageBuildResult,
    ValidationEvidence,
)
from ._image_build_redaction import failure_text
from ._json_input import parse_json
from ._service_descriptions import (
    SERVICE_REPOSITORY_ROOT,
    ServiceRepositoryManifest,
    capture_service_repository,
    parse_service_repository,
)
from ._yaml_input import InputError, parse_yaml, validate_contract

if TYPE_CHECKING:
    from pydantic import BaseModel

    from ._adapter_kit import Invocation
    from ._cv_run import CountingOutput, Placement, Robot, RunOutcome
    from ._cv_verdict import Runner
    from ._domain_contracts import ImageRecord
    from ._service_descriptions import ServiceDescription

ENGINE_URL = "tcp://cv-engine:2375"
ROBOTS: dict[str, Robot] = {
    "turtlebot3-waffle": "waffle_pi",
    "turtlebot3-burger": "burger_cam",
}


@dataclass(frozen=True)
class _Inputs:
    blueprint: CompositionBlueprint
    deployment: DeploymentSchema
    image_build: ImageBuildResult
    robot: Robot
    services: tuple[Composed, ...]


def validate(invocation: Invocation, *, engine_url: str) -> Verdict:
    inputs = _read(invocation)
    if isinstance(inputs, CvError):
        return _errored(invocation, inputs)
    invocation.diagnostic.fact("robot", inputs.robot)
    judgement = assess(inputs.services, _runner(invocation, inputs.robot, engine_url))
    if isinstance(judgement, CvError):
        return _errored(invocation, judgement)
    evidence = _evidence(invocation, inputs, judgement)
    size = len(encode_document(evidence))
    limit = next(
        item.max_bytes
        for item in invocation.request.outputs
        if item.slot == "validation_evidence"
    )
    if size > limit:
        return _errored(
            invocation,
            CvError(
                "evidence-too-large", f"the evidence is {size} bytes, over {limit}"
            ),
        )
    describe(invocation.diagnostic, judgement)
    return Verdict(judgement.outcome, {"validation_evidence": evidence})


def _errored(invocation: Invocation, error: CvError) -> Verdict:
    describe(invocation.diagnostic, error)
    return Verdict(error.outcome)


def _read(invocation: Invocation) -> _Inputs | CvError:
    declared = {item.slot: item for item in invocation.request.inputs}

    def document[ModelT: BaseModel](model: type[ModelT], slot: str) -> ModelT:
        content = invocation.inputs[slot]
        path = declared[slot].path
        return validate_contract(
            model, parse_json(content, path, max_bytes=len(content)), path
        )

    manifest = document(ServiceRepositoryManifest, "service_repository")
    snapshot = capture_service_repository(
        invocation.input_root / SERVICE_REPOSITORY_ROOT
    )
    if snapshot.manifest != manifest:
        msg = "copied service folders do not match the service-repository manifest"
        raise InputError(msg)
    profile_path = declared["target_profile"].path
    profile = validate_contract(
        TargetExecutionProfile,
        parse_yaml(invocation.inputs["target_profile"], profile_path),
        profile_path,
    )
    blueprint = document(CompositionBlueprint, "composition_blueprint")
    deployment = document(DeploymentSchema, "deployment_schema")
    image_build = document(ImageBuildResult, "image_build_result")
    composed = {item.slot: item.sha256 for item in blueprint.source_inputs}
    if composed.get("service_repository") != declared["service_repository"].sha256:
        return CvError(
            "input-mismatch",
            "the blueprint names another service repository digest",
        )
    target = profile.target.target_id
    robot = ROBOTS.get(target)
    if robot is None:
        return CvError("unsupported-robot", f"the profile targets {target}")
    descriptions = {
        item.description.service_id: item.description
        for item in parse_service_repository(snapshot.files)
    }
    images = {item.service_id: item for item in image_build.images}
    return _Inputs(
        blueprint,
        deployment,
        image_build,
        robot,
        tuple(
            _composed(item.service_id, descriptions, images)
            for item in blueprint.services
        ),
    )


def _composed(
    service_id: str,
    descriptions: dict[str, ServiceDescription],
    images: dict[str, ImageRecord],
) -> Composed:
    description = descriptions.get(service_id)
    if description is None:
        msg = f"{service_id}: the service repository has no description of it"
        raise InputError(msg)
    return Composed(
        service_id,
        images.get(service_id),
        frozenset(description.devices or ()),
        {topic: item.type for topic, item in (description.publishes or {}).items()},
    )


def _runner(invocation: Invocation, robot: Robot, engine_url: str) -> Runner:
    def run(
        placements: tuple[Placement, ...], counting: tuple[CountingOutput, ...]
    ) -> RunOutcome | CvError:
        from docker.errors import DockerException  # noqa: PLC0415

        request = RunRequest(
            invocation.request.correlation.execution_id,
            robot,
            placements,
            counting,
            invocation.deadline_monotonic,
        )
        try:
            return run_composition(request, engine_url)
        except (DockerException, *ENGINE_LOST) as error:
            return CvError(
                "engine-unreachable",
                failure_text(str(error), fallback=type(error).__name__),
            )

    return run


def _evidence(
    invocation: Invocation, inputs: _Inputs, judgement: Judgement
) -> ValidationEvidence:
    deployment = inputs.deployment
    mission = judgement.mission
    return validate_contract(
        ValidationEvidence,
        {
            "schema_version": "sdi.validation-evidence/v1",
            "evidence_basis": "implemented",
            "validation_evidence_id": (
                f"{deployment.scenario_id}-{deployment.testcase_id}-"
                f"{deployment.combination_id}-cv"
            ).lower(),
            "blueprint_id": inputs.blueprint.blueprint_id,
            "deployment_schema_id": deployment.deployment_schema_id,
            "image_build_result_id": inputs.image_build.image_build_result_id,
            "scenario_id": deployment.scenario_id,
            "testcase_id": deployment.testcase_id,
            "combination_id": deployment.combination_id,
            "profile_id": deployment.profile_id,
            "source_inputs": [
                {"slot": item.slot, "sha256": item.sha256}
                for item in invocation.request.inputs
            ],
            "domain_outcome": judgement.outcome.domain_outcome,
            "kpi_evaluation": "not_evaluated",
            "negative_cases": "not_evaluated",
            "platform": "linux/amd64",
            "services": [
                item.model_dump(mode="json", exclude_none=True)
                for item in judgement.services
            ],
        }
        | (
            {}
            if mission is None
            else {"mission": mission.model_dump(mode="json", exclude_none=True)}
        ),
        "validation-evidence.json",
    )


def warm_engine(engine_url: str) -> int:
    from docker.errors import DockerException  # noqa: PLC0415

    _say("sdi-cv-warm: checking the CV engine for the S-04 sim image")
    started = time.monotonic()
    try:
        prepared = prepare_engine(engine_url, _forward_step)
    except (DockerException, *ENGINE_LOST) as error:
        detail = failure_text(str(error), fallback=type(error).__name__)
        sys.stderr.write(f"sdi-cv-warm: engine-unreachable: {detail}\n")
        return 1
    match prepared:
        case SimBuildFailed(detail=detail):
            detail = failure_text(detail, fallback="the build failed")
            sys.stderr.write(f"sdi-cv-warm: sim-build-failed: {detail}\n")
            return 1
        case SimImage(tag=tag, built=built):
            seconds = round(time.monotonic() - started, 1)
            _say(f"sdi-cv-warm: {tag} {'built' if built else 'present'} in {seconds} s")
            return 0


def _forward_step(text: str) -> None:
    if text.startswith("Step "):
        _say(text.rstrip("\n"))


def _say(line: str) -> None:
    sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


main = serve("cv", partial(validate, engine_url=ENGINE_URL))
warm = partial(warm_engine, ENGINE_URL)
