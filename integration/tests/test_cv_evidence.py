"""The Validation evidence contract that carries CV's verdict."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from sdi_pipeline_integration._domain_contracts import ValidationEvidence
from tests._cv_evidence import (
    FAILED,
    NOT_EVALUATED,
    SERVICE_IDS,
    SUCCEEDED,
    evidence,
    mission,
    participated,
)


def _validate(document: dict[str, Any]) -> ValidationEvidence:
    return ValidationEvidence.model_validate(document, strict=True, extra="forbid")


def _with_service(
    document: dict[str, Any], index: int, service: dict[str, Any]
) -> dict[str, Any]:
    changed = copy.deepcopy(document)
    changed["services"][index] = service
    return changed


def _without(document: dict[str, Any], field: str) -> dict[str, Any]:
    return {name: value for name, value in document.items() if name != field}


SILENT = {
    "service_id": "fixture-navigation",
    "participation": "did_not_participate",
    "reason": "no-message",
}
TIMED_OUT_SETUP = {
    "setup": "timed_out",
    "steps": [step | {"status": "not_reached"} for step in mission()["steps"]],
}


@pytest.mark.parametrize(
    "document",
    [
        SUCCEEDED,
        FAILED,
        evidence(
            "failed",
            [participated(item) for item in SERVICE_IDS],
            mission("verify-recipient-face"),
        ),
        evidence(
            "failed", [participated(item) for item in SERVICE_IDS], TIMED_OUT_SETUP
        ),
        NOT_EVALUATED,
    ],
    ids=["succeeded", "failed-service", "failed-step", "failed-setup", "not-evaluated"],
)
def test_cv_evidence_carries_each_verdict(document: dict[str, Any]) -> None:
    assert _validate(document).domain_outcome == document["domain_outcome"]


@pytest.mark.parametrize(
    "service",
    [
        SILENT | {"reason": "failed-to-start"},
        SILENT | {"reason": "container-exited", "exit_code": 137},
    ],
    ids=["failed-to-start", "container-exited"],
)
def test_cv_evidence_names_why_a_service_did_not_participate(
    service: dict[str, Any],
) -> None:
    _validate(_with_service(FAILED, 1, service))


@pytest.mark.parametrize(
    "reason",
    [
        "image-unavailable",
        "no-amd64",
        "needs-gpu-or-device",
        "unsupported-command",
        "no-counting-output",
        "unloadable-type",
    ],
)
def test_cv_evidence_names_each_pipeline_gap(reason: str) -> None:
    gap = NOT_EVALUATED["services"][2] | {"reason": reason}

    _validate(_with_service(NOT_EVALUATED, 2, gap))


@pytest.mark.parametrize(
    ("document", "rule"),
    [
        (
            _with_service(
                SUCCEEDED, 1, participated(SERVICE_IDS[1]) | {"reason": "no-message"}
            ),
            "Extra inputs are not permitted",
        ),
        (
            _with_service(
                SUCCEEDED, 1, participated(SERVICE_IDS[1]) | {"detail": "late"}
            ),
            "Extra inputs are not permitted",
        ),
        (
            _with_service(FAILED, 1, SILENT | {"exit_code": 1}),
            "exit_code is recorded exactly for a container-exited service",
        ),
        (
            _with_service(FAILED, 1, SILENT | {"reason": "container-exited"}),
            "exit_code is recorded exactly for a container-exited service",
        ),
        (_with_service(FAILED, 1, SILENT | {"reason": "crashed"}), "reason"),
        (_with_service(FAILED, 1, SILENT | {"reason": "blocked"}), "reason"),
        (
            NOT_EVALUATED | {"mission": mission()},
            "evidence holds a mission exactly when the services were evaluated",
        ),
        (
            _without(SUCCEEDED, "mission"),
            "evidence holds a mission exactly when the services were evaluated",
        ),
        (
            FAILED | {"mission": TIMED_OUT_SETUP | {"steps": mission()["steps"]}},
            "a mission whose setup timed out reaches no step",
        ),
        (
            SUCCEEDED
            | {
                "mission": {
                    "setup": "passed",
                    "steps": [
                        step | {"waited_on": "/book"} for step in mission()["steps"]
                    ],
                }
            },
            "a scripted step waits on nothing",
        ),
        (
            _with_service(SUCCEEDED, 1, SILENT),
            "services and mission make the domain outcome failed",
        ),
        (
            SUCCEEDED | {"mission": mission("return-to-origin", "failed")},
            "services and mission make the domain outcome failed",
        ),
        (
            SUCCEEDED | {"domain_outcome": "failed"},
            "services and mission make the domain outcome succeeded",
        ),
        (
            _with_service(NOT_EVALUATED, 0, participated(SERVICE_IDS[0])),
            "a pipeline gap leaves every service not_evaluated",
        ),
        (
            _with_service(
                NOT_EVALUATED, 2, NOT_EVALUATED["services"][2] | {"reason": "blocked"}
            ),
            "with at least one cause besides blocked",
        ),
        (
            _with_service(SUCCEEDED, 1, participated(SERVICE_IDS[0])),
            "service_id values must be unique",
        ),
        (SUCCEEDED | {"evidence_basis": "fixture"}, "evidence_basis"),
        (SUCCEEDED | {"platform": "linux/arm64"}, "platform"),
        (
            SUCCEEDED | {"validation_records": [{"validation_case_id": "navigation"}]},
            "Extra inputs are not permitted",
        ),
    ],
    ids=[
        "participating-with-reason",
        "participating-with-detail",
        "exit-code-without-container-exited",
        "container-exited-without-exit-code",
        "reason-outside-the-closed-set",
        "gap-reason-for-a-silent-service",
        "mission-when-not-evaluated",
        "no-mission-when-evaluated",
        "setup-timed-out-reaches-a-step",
        "scripted-step-waits",
        "succeeded-with-a-silent-service",
        "succeeded-with-a-failed-step",
        "failed-with-everything-met",
        "gap-beside-a-participating-service",
        "gap-with-only-blocked-services",
        "duplicate-service",
        "fixture-basis",
        "other-platform",
        "fixture-validation-records",
    ],
)
def test_cv_evidence_rejects_a_broken_rule(document: dict[str, Any], rule: str) -> None:
    with pytest.raises(ValidationError, match=rule):
        _validate(document)
