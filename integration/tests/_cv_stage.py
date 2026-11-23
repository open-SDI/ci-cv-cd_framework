from __future__ import annotations

import hashlib
import json
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sdi_pipeline_integration._domain_contracts import ValidationEvidence
from sdi_pipeline_integration._json_input import canonical_json
from sdi_pipeline_integration._service_descriptions import (
    SERVICE_REPOSITORY_ROOT,
    capture_service_repository,
)
from sdi_pipeline_integration._stage_contracts import (
    AdapterResponse,
    StageProfile,
    contains_sensitive_contract_material,
    contains_sensitive_material,
)
from sdi_pipeline_integration._stage_registry import STAGES
from sdi_pipeline_integration._yaml_input import parse_yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

SOURCE_ROOT = Path(__file__).resolve().parents[2]
CASE = SOURCE_ROOT / "integration/fixtures/cases/composition-s-04-tc-03-c-05"
PROFILE = SOURCE_ROOT / "integration/stage-profiles/cv-v1.yaml"
REQUIREMENTS = SOURCE_ROOT / "requirements/s-04/deliver-book-to-joe.md"
WAFFLE = SOURCE_ROOT / "profiles/s-04/waffle-xycar-amd64.yaml"
BURGER = SOURCE_ROOT / "profiles/s-04/burger-xycar-amd64.yaml"
EXECUTION_ID = "0b6f5d1e-2c3a-4d5e-8f60-718293a4b5c6"
IMAGE_BUILD_SLOTS = (
    "target_profile",
    "service_repository",
    "composition_blueprint",
    "deployment_schema",
)


def refusing_engine_url() -> str:
    """Return an engine URL that refuses connections at once.

    Some hosts drop rather than refuse port 1, which would stall a run that
    wrongly reached the engine for the client's whole timeout.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"tcp://127.0.0.1:{port}"


def description(
    service_id: str,
    publishes: Mapping[str, str] | None = None,
    devices: Sequence[str] | None = None,
) -> dict[str, Any]:
    front_matter: dict[str, Any] = {
        "schema_version": "sdi.service-description/v1",
        "service_id": service_id,
        "title": service_id,
        "provenance": {"basis": "placeholder", "note": "A CV test."},
    }
    if publishes is not None:
        front_matter["publishes"] = {
            topic: {"type": kind} for topic, kind in publishes.items()
        }
    if devices is not None:
        front_matter["devices"] = list(devices)
    return front_matter


@dataclass(frozen=True)
class CvRun:
    response: AdapterResponse
    evidence: ValidationEvidence | None
    diagnostic: str


class CvStage:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.request = root / "request.json"
        self.input_root = root / "inputs"
        self.output_root = root / "candidate"

    def write(  # noqa: PLR0913
        self,
        services: Mapping[str, Path | dict[str, Any]],
        images: Sequence[Mapping[str, Any]],
        *,
        profile: Path = WAFFLE,
        target_id: str | None = None,
        blueprint_repository_digest: str | None = None,
        evidence_max_bytes: int | None = None,
        execution_id: str = EXECUTION_ID,
    ) -> None:
        repository = self.input_root / SERVICE_REPOSITORY_ROOT
        for service_id, source in services.items():
            folder = repository / service_id
            if isinstance(source, Path):
                shutil.copytree(source, folder)
            else:
                folder.mkdir(parents=True)
                (folder / "SDI.md").write_text(
                    f"---\n{json.dumps(source)}\n---\n\n# {service_id}\n"
                )
        manifest = capture_service_repository(repository).manifest
        repository_bytes = canonical_json(manifest.model_dump(mode="json"))
        repository_digest = hashlib.sha256(repository_bytes).hexdigest()
        target_profile = profile.read_bytes()
        if target_id is not None:
            document = parse_yaml(target_profile, str(profile))
            assert isinstance(document, dict)
            document["target"] = {"target_id": target_id, "kind": "sdr"}
            target_profile = canonical_json(document)
        blueprint = json.loads((CASE / "composition-blueprint.json").read_text())
        blueprint |= {
            "evidence_basis": "implemented",
            "services": [
                {
                    "service_id": service_id,
                    "basis": "declared",
                    "capability": service_id,
                    "depends_on": [],
                }
                for service_id in services
            ],
            "source_inputs": [
                item
                | (
                    {"sha256": blueprint_repository_digest or repository_digest}
                    if item["slot"] == "service_repository"
                    else {}
                )
                for item in blueprint["source_inputs"]
            ],
        }
        deployment = json.loads((CASE / "deployment-schema.json").read_text())
        deployment |= {
            "evidence_basis": "implemented",
            "placements": [
                {
                    "service_id": service_id,
                    "status": "resolved",
                    "artifact_id": "jazzy-debs",
                    "host": "xycar",
                }
                for service_id in services
            ],
            "source_inputs": blueprint["source_inputs"],
        }
        image_build = {
            "schema_version": "sdi.image-build-result/v1",
            "evidence_basis": "implemented",
            "image_build_result_id": "s-04-tc-03-c-05-image-build",
            "blueprint_id": blueprint["blueprint_id"],
            "deployment_schema_id": deployment["deployment_schema_id"],
            "scenario_id": "S-04",
            "testcase_id": "TC-03",
            "combination_id": "C-05",
            "profile_id": deployment["profile_id"],
            "source_inputs": [
                {"slot": slot, "sha256": "0" * 64} for slot in IMAGE_BUILD_SLOTS
            ],
            "registry": "registry:5000",
            "images": list(images),
        }
        contents = {
            "requirements_specification": REQUIREMENTS.read_bytes(),
            "target_profile": target_profile,
            "service_repository": repository_bytes,
            "composition_blueprint": canonical_json(blueprint),
            "deployment_schema": canonical_json(deployment),
            "image_build_result": canonical_json(image_build),
        }
        stage_profile = StageProfile.model_validate(
            parse_yaml(PROFILE.read_bytes(), str(PROFILE))
        )
        inputs: list[dict[str, Any]] = []
        for grant in stage_profile.inputs:
            content = contents[grant.slot]
            (self.input_root / grant.path).write_bytes(content)
            inputs.append(
                grant.model_dump(mode="json")
                | {
                    "byte_size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        outputs = [item.model_dump(mode="json") for item in stage_profile.outputs]
        if evidence_max_bytes is not None:
            outputs = [item | {"max_bytes": evidence_max_bytes} for item in outputs]
        request = {
            "schema_version": "sdi.stage-adapter-request/v1",
            "correlation": {
                "execution_id": execution_id,
                "stage": "cv",
                "attempt_number": 1,
            },
            "work_limit_seconds": stage_profile.work_limit_seconds,
            "inputs": inputs,
            "outputs": outputs,
            "diagnostic": stage_profile.diagnostic.model_dump(mode="json"),
        }
        self.request.write_bytes(canonical_json(request))

    def argv(self) -> list[str]:
        self.output_root.mkdir(exist_ok=True)
        return [
            "run",
            "--request",
            str(self.request),
            "--input-root",
            str(self.input_root),
            "--output-root",
            str(self.output_root),
        ]

    def run(self, main: Callable[[Sequence[str]], int]) -> CvRun:
        return self.published(main(self.argv()))

    def published(self, exit_code: int) -> CvRun:
        assert exit_code == 0
        response = AdapterResponse.model_validate_json(
            (self.output_root / "response.json").read_bytes(), strict=False
        )
        evidence_path = self.output_root / "outputs/validation-evidence.json"
        evidence = None
        if evidence_path.exists():
            assert not contains_sensitive_contract_material(
                json.loads(evidence_path.read_bytes())
            )
            evidence = ValidationEvidence.model_validate_json(
                evidence_path.read_bytes()
            )
        diagnostic = (self.output_root / "diagnostic.txt").read_text()
        assert not contains_sensitive_material(diagnostic)
        assert len(diagnostic.encode()) <= STAGES["cv"].grant.diagnostic_max_bytes
        return CvRun(response, evidence, diagnostic)
