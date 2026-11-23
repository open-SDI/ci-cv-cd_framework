"""One consistent combination for pipeline-event rows: its run, profile and attempts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from sdi_pipeline_integration._pipeline_event_rows import build_row
from sdi_pipeline_integration._stage_registry import EXPECTED_STAGE_INPUT_SLOTS
from sdi_pipeline_integration._stage_runtime import StageInputSource
from tests._cv_evidence import evidence, mission
from tests._pipeline_events import EXECUTION_ID
from tests._stage_attempts import attempt

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sdi_pipeline_integration._pipeline_event_rows import EventLine, LineHead
    from sdi_pipeline_integration._pipeline_events import (
        EventScope,
        PipelineEventRow,
    )
    from sdi_pipeline_integration._stage_contracts import ExecutionConclusion
    from sdi_pipeline_integration._stage_registry import StageName
    from sdi_pipeline_integration._stage_runtime import StageExecution
    from tests._stage_attempts import Termination

PROFILES: Final = Path(__file__).parents[2] / "profiles" / "s-04"
SERVICE_REPOSITORY: Final = Path(__file__).parents[2] / "service-repository"
IDENTIFIED: Final[dict[str, Any]] = {
    "execution_id": EXECUTION_ID,
    "scenario_id": "S-04",
    "testcase_id": "TC-03",
    "combination_id": "C-05",
    "profile_id": "waffle-jetson-arm64",
}
EMITTED_AT: Final = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


def profile_source(
    name: str = "waffle-jetson-arm64", *, content: bytes | None = None
) -> StageInputSource:
    """Return a committed target profile as every Stage receives it."""
    return StageInputSource(
        source_path=f"profiles/s-04/{name}.yaml",
        media_type="application/yaml",
        schema_version="sdi.target-execution-profile/v1",
        content=(PROFILES / f"{name}.yaml").read_bytes()
        if content is None
        else content,
    )


def build(  # noqa: PLR0913
    executions: Sequence[StageExecution] = (),
    *,
    line: LineHead,
    live_items: Sequence[EventLine] = (),
    scope: EventScope = "run",
    profile: StageInputSource | None = None,
    emitted_at: datetime = EMITTED_AT,
) -> PipelineEventRow:
    """Return `build_row` for this combination's run."""
    return build_row(
        IDENTIFIED,
        profile or profile_source(),
        executions,
        line=line,
        live_items=live_items,
        scope=scope,
        emitted_at=emitted_at,
    )


@dataclass(frozen=True)
class Placement:
    """One service placed as an artifact on a host of waffle-jetson-arm64."""

    service_id: str
    artifact_id: str
    host: str


PLACEMENTS: Final = (
    Placement("nav2-localization", "jazzy-debs", "waffle"),
    Placement("nav2-navigation", "jazzy-debs", "waffle"),
    Placement("face-recog", "jazzy-source", "orin"),
)


def _document(
    schema_version: str, stage: StageName, fields: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "blueprint_id": "s-04-tc-03-c-05-composition",
        "scenario_id": "S-04",
        "testcase_id": "TC-03",
        "combination_id": "C-05",
        "profile_id": "waffle-jetson-arm64",
        "source_inputs": [
            {"slot": slot, "sha256": "0" * 64}
            for slot in EXPECTED_STAGE_INPUT_SLOTS[stage]
        ],
        **fields,
    }


def blueprint(
    placements: Sequence[Placement] = PLACEMENTS, *, basis: str = "implemented"
) -> dict[str, Any]:
    requirements = [step["requirement_id"] for step in mission()["steps"]]
    return _document(
        "sdi.composition-blueprint/v1",
        "composition",
        {
            "evidence_basis": basis,
            "proposal_id": "proposal-1",
            "profile_basis": "declared",
            "requirement_ids": requirements,
            "services": [
                {
                    "service_id": item.service_id,
                    "basis": "declared",
                    "capability": "navigation",
                    "depends_on": [],
                }
                for item in placements
            ],
            "coverage": [
                {"requirement_id": requirement, "status": "missing", "services": []}
                for requirement in requirements
            ],
        },
    )


def deployment_schema(
    placements: Sequence[Placement] = PLACEMENTS, *, basis: str = "implemented"
) -> dict[str, Any]:
    return _document(
        "sdi.deployment-schema/v1",
        "composition",
        {
            "evidence_basis": basis,
            "deployment_schema_id": "s-04-tc-03-c-05-deployment",
            "profile_basis": "declared",
            "placements": [
                {
                    "service_id": item.service_id,
                    "status": "resolved",
                    "artifact_id": item.artifact_id,
                    "host": item.host,
                }
                if basis == "implemented"
                else {"service_id": item.service_id, "status": "unresolved"}
                for item in placements
            ],
        },
    )


def digest(name: str) -> str:
    return f"sha256:{hashlib.sha256(name.encode()).hexdigest()}"


def built(service_id: str) -> dict[str, Any]:
    return {
        "service_id": service_id,
        "status": "built",
        "repository": f"sdi/{service_id}",
        "digest": digest(f"{service_id}/index"),
        "platforms": [
            {"architecture": "amd64", "digest": digest(f"{service_id}/amd64")},
            {"architecture": "arm64", "digest": digest(f"{service_id}/arm64")},
        ],
    }


def image_build_result(
    placements: Sequence[Placement] = PLACEMENTS,
    *,
    failed: Mapping[str, str] | None = None,
    basis: str = "implemented",
) -> dict[str, Any]:
    """Return an image-build result; `failed` maps a service id to its reason."""
    failed = failed or {}
    images = [
        {"service_id": item.service_id, "status": "not_evaluated"}
        if basis == "fixture"
        else {
            "service_id": item.service_id,
            "status": "failed",
            "reason": failed[item.service_id],
            "detail": "process did not complete successfully: exit code: 100",
        }
        if item.service_id in failed
        else built(item.service_id)
        for item in placements
    ]
    return _document(
        "sdi.image-build-result/v1",
        "image_build",
        {
            "evidence_basis": basis,
            "image_build_result_id": "s-04-tc-03-c-05-image-build",
            "deployment_schema_id": "s-04-tc-03-c-05-deployment",
            "registry": "registry:5000",
            "images": images,
        },
    )


def participated(service_id: str) -> dict[str, Any]:
    return {
        "service_id": service_id,
        "participation": "participated",
        "image_digest": digest(f"{service_id}/amd64"),
        "counting_topics": [{"topic": f"/{service_id}/state", "messages": 412}],
    }


def validation_evidence(
    placements: Sequence[Placement] = PLACEMENTS,
    *,
    domain_outcome: str = "succeeded",
    services: Sequence[dict[str, Any]] | None = None,
    steps: dict[str, Any] | None = None,
    evaluated: bool = True,
) -> dict[str, Any]:
    """Return CV evidence; every service participates unless `services` says."""
    chosen = (
        [participated(item.service_id) for item in placements]
        if services is None
        else list(services)
    )
    return evidence(domain_outcome, chosen, (steps or mission()) if evaluated else None)


def deployed(item: Placement) -> dict[str, Any]:
    image = f"registry:5000/sdi/{item.service_id}@{digest(f'{item.service_id}/arm64')}"
    return {
        "service_id": item.service_id,
        "host": item.host,
        "status": "deployed",
        "node": item.host,
        "image": image,
        "image_id": image,
        "phase": "Running",
        "restart_count": 0,
    }


def deployment_result(
    placements: Sequence[Placement] = PLACEMENTS,
    *,
    failed: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """Return a CD result; `failed` maps a service id to its k8s_reason or None."""
    failed = failed or {}
    records = [
        deployed(item)
        if item.service_id not in failed
        else deployed(item)
        | {
            "status": "failed",
            "restart_count": 4,
            "detail": "back-off 1m20s restarting failed container",
        }
        | (
            {}
            if failed[item.service_id] is None
            else {"k8s_reason": failed[item.service_id]}
        )
        for item in placements
    ]
    return _document(
        "sdi.deployment-result/v1",
        "cd",
        {
            "evidence_basis": "implemented",
            "deployment_result_id": "s-04-tc-03-c-05-deployment-result",
            "deployment_schema_id": "s-04-tc-03-c-05-deployment",
            "image_build_result_id": "s-04-tc-03-c-05-image-build",
            "domain_outcome": "failed" if failed else "succeeded",
            "cluster_kind": "stand_in",
            "placements": records,
            "manifest": {"apiVersion": "v1", "kind": "List", "items": []},
        },
    )


def composition_ok(placements: Sequence[Placement] = PLACEMENTS) -> StageExecution:
    return attempt(
        "composition",
        documents={
            "composition_blueprint": blueprint(placements),
            "deployment_schema": deployment_schema(placements),
        },
    )


def images_built(
    placements: Sequence[Placement] = PLACEMENTS,
    *,
    failed: Mapping[str, str] | None = None,
) -> StageExecution:
    return attempt(
        "image_build",
        documents={"image_build_result": image_build_result(placements, failed=failed)},
    )


def cv_passed(placements: Sequence[Placement] = PLACEMENTS) -> StageExecution:
    return attempt(
        "cv", documents={"validation_evidence": validation_evidence(placements)}
    )


def cd_deployed(placements: Sequence[Placement] = PLACEMENTS) -> StageExecution:
    return attempt("cd", documents={"deployment_result": deployment_result(placements)})


def domain_failed(
    stage: StageName, code: str, documents: Mapping[str, Any] | None = None
) -> StageExecution:
    return attempt(stage, domain="failed", reason=("domain", code), documents=documents)


def adapter_failed(
    stage: StageName,
    code: str,
    *,
    conclusion: ExecutionConclusion = "failed",
    termination: Termination = "exited",
    exit_code: int = 1,
) -> StageExecution:
    return attempt(
        stage,
        conclusion=conclusion,
        domain="not_evaluated",
        reason=("adapter", code),
        termination=termination,
        exit_code=exit_code,
    )


def fixture_composition() -> StageExecution:
    return attempt(
        "composition",
        mode="fixture",
        domain="not_evaluated",
        reason=("fixture", "sdi.fixture.composition-executed"),
        documents={
            "composition_blueprint": blueprint(basis="fixture"),
            "deployment_schema": deployment_schema(basis="fixture"),
        },
    )


def fixture_images() -> StageExecution:
    return attempt(
        "image_build",
        mode="fixture",
        domain="not_evaluated",
        reason=("fixture", "sdi.fixture.image-build-executed"),
        documents={"image_build_result": image_build_result(basis="fixture")},
    )


def cv_mission_failed() -> StageExecution:
    steps = mission("navigate-to-rendezvous", "failed")
    return domain_failed(
        "cv",
        "sdi.cv.failed",
        {
            "validation_evidence": validation_evidence(
                domain_outcome="failed", steps=steps
            )
        },
    )


def cv_service_absent() -> StageExecution:
    silent = {
        "service_id": PLACEMENTS[2].service_id,
        "participation": "did_not_participate",
        "reason": "no-message",
    }
    services = [participated(item.service_id) for item in PLACEMENTS[:2]]
    return domain_failed(
        "cv",
        "sdi.cv.failed",
        {
            "validation_evidence": validation_evidence(
                domain_outcome="failed", services=[*services, silent]
            )
        },
    )


def cv_not_evaluated() -> StageExecution:
    services = [
        {
            "service_id": item.service_id,
            "participation": "not_evaluated",
            "reason": "blocked",
        }
        for item in PLACEMENTS[:2]
    ]
    unavailable = {
        "service_id": PLACEMENTS[2].service_id,
        "participation": "not_evaluated",
        "reason": "image-unavailable",
    }
    return attempt(
        "cv",
        domain="not_evaluated",
        reason=("domain", "sdi.cv.not-evaluated"),
        documents={
            "validation_evidence": validation_evidence(
                domain_outcome="not_evaluated",
                services=[*services, unavailable],
                evaluated=False,
            )
        },
    )
