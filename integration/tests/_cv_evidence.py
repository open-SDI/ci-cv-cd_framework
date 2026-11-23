from __future__ import annotations

from typing import Any

SERVICE_IDS = ("fixture-localization", "fixture-navigation", "fixture-mission-control")
AMD64_DIGEST = f"sha256:{'c' * 64}"
WAITED_ON = {
    "secure-book": None,
    "accept-spoken-request": "/speech/text",
    "resolve-rendezvous": None,
    "navigate-to-rendezvous": "/navigate_to_pose",
    "verify-recipient-face": "/face_recognition/name",
    "transfer-book": None,
    "acknowledge-receipt": "/speech/text",
    "return-to-origin": "/navigate_to_pose",
}
CV_SLOTS = (
    "requirements_specification",
    "target_profile",
    "service_repository",
    "composition_blueprint",
    "deployment_schema",
    "image_build_result",
)


def step(requirement_id: str, status: str) -> dict[str, Any]:
    waited_on = WAITED_ON[requirement_id]
    return {"requirement_id": requirement_id, "status": status} | (
        {} if waited_on is None else {"waited_on": waited_on}
    )


def mission(
    first_unmet: str | None = None, unmet_status: str = "timed_out"
) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    reached = True
    for requirement_id, waited_on in WAITED_ON.items():
        if requirement_id == first_unmet:
            reached = False
            steps.append(step(requirement_id, unmet_status))
        elif not reached:
            steps.append(step(requirement_id, "not_reached"))
        else:
            steps.append(
                step(requirement_id, "scripted" if waited_on is None else "passed")
            )
    return {"setup": "passed", "steps": steps}


def participated(service_id: str) -> dict[str, Any]:
    return {
        "service_id": service_id,
        "participation": "participated",
        "image_digest": AMD64_DIGEST,
        "counting_topics": [{"topic": f"/{service_id}/state", "messages": 412}],
    }


def evidence(
    domain_outcome: str,
    services: list[dict[str, Any]],
    mission: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema_version": "sdi.validation-evidence/v1",
        "evidence_basis": "implemented",
        "validation_evidence_id": "s-04-tc-03-c-05-validation",
        "blueprint_id": "s-04-tc-03-c-05-composition",
        "deployment_schema_id": "s-04-tc-03-c-05-deployment",
        "image_build_result_id": "s-04-tc-03-c-05-image-build",
        "scenario_id": "S-04",
        "testcase_id": "TC-03",
        "combination_id": "C-05",
        "profile_id": "waffle-jetson-arm64",
        "source_inputs": [{"slot": slot, "sha256": "0" * 64} for slot in CV_SLOTS],
        "domain_outcome": domain_outcome,
        "kpi_evaluation": "not_evaluated",
        "negative_cases": "not_evaluated",
        "platform": "linux/amd64",
        "services": services,
    } | ({} if mission is None else {"mission": mission})


SUCCEEDED = evidence(
    "succeeded", [participated(item) for item in SERVICE_IDS], mission()
)
FAILED = evidence(
    "failed",
    [
        participated("fixture-localization"),
        {
            "service_id": "fixture-navigation",
            "participation": "did_not_participate",
            "reason": "container-exited",
            "exit_code": 1,
            "image_digest": AMD64_DIGEST,
        },
        participated("fixture-mission-control"),
    ],
    mission(),
)
NOT_EVALUATED = evidence(
    "not_evaluated",
    [
        {
            "service_id": "fixture-localization",
            "participation": "not_evaluated",
            "reason": "blocked",
        },
        {
            "service_id": "fixture-navigation",
            "participation": "not_evaluated",
            "reason": "blocked",
        },
        {
            "service_id": "fixture-mission-control",
            "participation": "not_evaluated",
            "reason": "unsupported-command",
            "detail": "the image command is neither ros2 launch nor ros2 run",
        },
    ],
    None,
)
