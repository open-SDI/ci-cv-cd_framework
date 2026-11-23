from __future__ import annotations

from typing import Any

HOSTS = {
    "fixture-localization": "orin",
    "fixture-navigation": "waffle",
    "fixture-mission-control": "orin",
}
AMD64_DIGEST = f"sha256:{'c' * 64}"
INDEX_DIGEST = f"sha256:{'d' * 64}"
ARM64_DIGEST = f"sha256:{'e' * 64}"
CD_SLOTS = (
    "target_profile",
    "composition_blueprint",
    "deployment_schema",
    "image_build_result",
)
MANIFEST: dict[str, Any] = {
    "apiVersion": "v1",
    "kind": "List",
    "items": [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": service_id},
        }
        for service_id in HOSTS
    ],
}


def image(service_id: str, digest: str = ARM64_DIGEST) -> str:
    return f"registry:5000/sdi/{service_id}@{digest}"


def deployed(service_id: str) -> dict[str, Any]:
    return {
        "service_id": service_id,
        "host": HOSTS[service_id],
        "status": "deployed",
        "node": HOSTS[service_id],
        "image": image(service_id),
        "image_id": image(service_id),
        "phase": "Running",
        "restart_count": 0,
    }


def crash_looping(service_id: str) -> dict[str, Any]:
    return deployed(service_id) | {
        "status": "failed",
        "restart_count": 4,
        "detail": "CrashLoopBackOff: back-off 1m20s restarting failed container",
    }


def undeployed(service_id: str, reason: str = "blocked") -> dict[str, Any]:
    return {
        "service_id": service_id,
        "host": HOSTS[service_id],
        "status": "not_evaluated",
        "reason": reason,
    }


def result(
    domain_outcome: str,
    placements: list[dict[str, Any]],
    manifest: dict[str, Any] | None = MANIFEST,
) -> dict[str, Any]:
    return {
        "schema_version": "sdi.deployment-result/v1",
        "evidence_basis": "implemented",
        "deployment_result_id": "s-04-tc-03-c-05-deployment-result",
        "blueprint_id": "s-04-tc-03-c-05-composition",
        "deployment_schema_id": "s-04-tc-03-c-05-deployment",
        "image_build_result_id": "s-04-tc-03-c-05-image-build",
        "scenario_id": "S-04",
        "testcase_id": "TC-03",
        "combination_id": "C-05",
        "profile_id": "waffle-jetson-arm64",
        "source_inputs": [{"slot": slot, "sha256": "0" * 64} for slot in CD_SLOTS],
        "domain_outcome": domain_outcome,
        "cluster_kind": "stand_in",
        "placements": placements,
    } | ({} if manifest is None else {"manifest": manifest})


SUCCEEDED = result("succeeded", [deployed(item) for item in HOSTS])
FAILED = result(
    "failed",
    [
        deployed("fixture-localization"),
        crash_looping("fixture-navigation"),
        deployed("fixture-mission-control"),
    ],
)
NOT_EVALUATED = result(
    "not_evaluated",
    [
        undeployed("fixture-localization"),
        undeployed("fixture-navigation", "no-image-for-architecture"),
        undeployed("fixture-mission-control"),
    ],
    manifest=None,
)
