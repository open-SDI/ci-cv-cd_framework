from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sdi_pipeline_integration._domain_contracts import DeploymentResult
from sdi_pipeline_integration._json_input import canonical_json
from sdi_pipeline_integration._stage_contracts import (
    AdapterResponse,
    StageProfile,
    contains_sensitive_material,
)
from sdi_pipeline_integration._stage_registry import STAGES
from sdi_pipeline_integration._yaml_input import parse_yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

SOURCE_ROOT = Path(__file__).resolve().parents[2]
CASE = SOURCE_ROOT / "integration/fixtures/cases/composition-s-04-tc-03-c-05"
STAGE_PROFILE = SOURCE_ROOT / "integration/stage-profiles/cd-v1.yaml"
PROFILES = SOURCE_ROOT / "profiles/s-04"
INDEX_DIGEST = f"sha256:{'1' * 64}"


def built(service_id: str, platforms: Mapping[str, str]) -> dict[str, Any]:
    return {
        "service_id": service_id,
        "status": "built",
        "repository": f"sdi/{service_id}",
        "digest": INDEX_DIGEST,
        "platforms": [
            {"architecture": name, "digest": digest}
            for name, digest in platforms.items()
        ],
    }


@dataclass(frozen=True)
class CdRun:
    response: AdapterResponse
    evidence: DeploymentResult | None
    diagnostic: str


class CdStage:
    def __init__(self, root: Path, execution_id: str) -> None:
        self.root = root
        self.execution_id = execution_id
        self.request = root / "request.json"
        self.input_root = root / "inputs"
        self.output_root = root / "candidate"

    @property
    def namespace(self) -> str:
        return f"sdi-{self.execution_id}"

    def write(
        self,
        placements: Mapping[str, str],
        images: Sequence[Mapping[str, Any]],
        *,
        profile: str,
        work_limit_seconds: int | None = None,
    ) -> None:
        target_profile = (PROFILES / f"{profile}.yaml").read_bytes()
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
                for service_id in placements
            ],
        }
        deployment = json.loads((CASE / "deployment-schema.json").read_text())
        deployment |= {
            "evidence_basis": "implemented",
            "profile_id": profile,
            "placements": [
                {
                    "service_id": service_id,
                    "status": "resolved",
                    "artifact_id": f"{service_id}-image",
                    "host": host,
                }
                for service_id, host in placements.items()
            ],
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
            "profile_id": profile,
            "source_inputs": [
                {"slot": slot, "sha256": "0" * 64}
                for slot in STAGES["image_build"].grant.inputs
            ],
            "registry": "registry:5000",
            "images": list(images),
        }
        contents = {
            "target_profile": target_profile,
            "composition_blueprint": canonical_json(blueprint),
            "deployment_schema": canonical_json(deployment),
            "image_build_result": canonical_json(image_build),
        }
        stage_profile = StageProfile.model_validate(
            parse_yaml(STAGE_PROFILE.read_bytes(), str(STAGE_PROFILE))
        )
        self.input_root.mkdir(parents=True)
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
        request = {
            "schema_version": "sdi.stage-adapter-request/v1",
            "correlation": {
                "execution_id": self.execution_id,
                "stage": "cd",
                "attempt_number": 1,
            },
            "work_limit_seconds": work_limit_seconds
            or stage_profile.work_limit_seconds,
            "inputs": inputs,
            "outputs": [item.model_dump(mode="json") for item in stage_profile.outputs],
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

    def run(self, main: Callable[[Sequence[str]], int]) -> CdRun:
        assert main(self.argv()) == 0
        response = AdapterResponse.model_validate_json(
            (self.output_root / "response.json").read_bytes(), strict=False
        )
        evidence_path = self.output_root / "outputs/deployment-result.json"
        evidence = (
            DeploymentResult.model_validate_json(evidence_path.read_bytes())
            if evidence_path.exists()
            else None
        )
        diagnostic = (self.output_root / "diagnostic.txt").read_text()
        assert not contains_sensitive_material(diagnostic)
        assert len(diagnostic.encode()) <= STAGES["cd"].grant.diagnostic_max_bytes
        return CdRun(response, evidence, diagnostic)
