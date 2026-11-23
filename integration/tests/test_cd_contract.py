"""The Deployment result contract that carries CD's outcome."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from sdi_pipeline_integration._domain_contracts import DeploymentResult
from tests._cd_evidence import (
    AMD64_DIGEST,
    FAILED,
    NOT_EVALUATED,
    SUCCEEDED,
    crash_looping,
    deployed,
    image,
    result,
    undeployed,
)


def _validate(document: dict[str, Any]) -> DeploymentResult:
    return DeploymentResult.model_validate(document, strict=True, extra="forbid")


def _with_placement(
    document: dict[str, Any], index: int, placement: dict[str, Any]
) -> dict[str, Any]:
    changed = copy.deepcopy(document)
    changed["placements"][index] = placement
    return changed


def _without(record: dict[str, Any], *fields: str) -> dict[str, Any]:
    return {name: value for name, value in record.items() if name not in fields}


PULL_FAILED = {
    "service_id": "fixture-navigation",
    "host": "waffle",
    "status": "failed",
    "detail": "ImagePullBackOff: Back-off pulling image",
    "image": image("fixture-navigation"),
    "phase": "Pending",
}
CRASH = crash_looping("fixture-navigation")


@pytest.mark.parametrize(
    "document",
    [
        SUCCEEDED,
        FAILED,
        _with_placement(FAILED, 1, PULL_FAILED),
        _with_placement(
            FAILED,
            1,
            {
                "service_id": "fixture-navigation",
                "host": "waffle",
                "status": "failed",
                "detail": "Unschedulable: 0/4 nodes are available",
            },
        ),
        _with_placement(
            FAILED,
            1,
            deployed("fixture-navigation")
            | {"status": "failed", "node": "orin", "detail": "scheduled on orin"},
        ),
        NOT_EVALUATED,
    ],
    ids=[
        "succeeded",
        "failed-crash-loop",
        "failed-image-pull",
        "failed-with-nothing-observed",
        "failed-wrong-node",
        "not-evaluated",
    ],
)
def test_cd_result_carries_each_outcome(document: dict[str, Any]) -> None:
    assert _validate(document).domain_outcome == document["domain_outcome"]


@pytest.mark.parametrize(
    "reason",
    [
        "unresolved-placement",
        "image-not-built",
        "no-image-for-architecture",
        "host-not-in-cluster",
    ],
)
def test_cd_result_names_each_pipeline_gap(reason: str) -> None:
    gap = undeployed("fixture-navigation", reason)

    _validate(_with_placement(NOT_EVALUATED, 1, gap))


def test_cd_result_omits_the_host_of_an_unresolved_placement() -> None:
    gap = _without(undeployed("fixture-navigation", "unresolved-placement"), "host")

    _validate(_with_placement(NOT_EVALUATED, 1, gap))


@pytest.mark.parametrize(
    ("document", "rule"),
    [
        (
            _with_placement(SUCCEEDED, 1, CRASH),
            "placements make the domain outcome failed",
        ),
        (
            FAILED | {"domain_outcome": "succeeded"},
            "placements make the domain outcome failed",
        ),
        (
            SUCCEEDED | {"domain_outcome": "not_evaluated"},
            "placements make the domain outcome succeeded",
        ),
        (
            _without(SUCCEEDED, "manifest"),
            "a result holds a manifest exactly when it was evaluated",
        ),
        (
            _without(FAILED, "manifest"),
            "a result holds a manifest exactly when it was evaluated",
        ),
        (
            NOT_EVALUATED | {"manifest": SUCCEEDED["manifest"]},
            "a result holds a manifest exactly when it was evaluated",
        ),
        (
            _with_placement(NOT_EVALUATED, 1, undeployed("fixture-navigation")),
            "with at least one cause besides blocked",
        ),
        (
            _with_placement(NOT_EVALUATED, 0, deployed("fixture-localization")),
            "a pipeline gap leaves every placement not_evaluated",
        ),
        (
            _with_placement(
                FAILED, 1, CRASH | {"detail": "CrashLoopBackOff: caf\u00e9"}
            ),
            "detail",
        ),
        (_with_placement(FAILED, 1, CRASH | {"detail": "x" * 257}), "detail"),
        (
            _with_placement(FAILED, 1, CRASH | {"detail": 'Error: "exit 1"'}),
            "detail",
        ),
        (
            _with_placement(FAILED, 1, CRASH | {"detail": "Error: C:\\bin"}),
            "detail",
        ),
        (_with_placement(FAILED, 1, CRASH | {"detail": ""}), "detail"),
        (_with_placement(FAILED, 1, _without(CRASH, "detail")), "detail"),
        (
            _with_placement(FAILED, 1, CRASH | {"node": None}),
            "node must be omitted when unknown",
        ),
        (
            _with_placement(FAILED, 1, CRASH | {"image_id": None}),
            "image_id must be omitted when unknown",
        ),
        (
            NOT_EVALUATED | {"manifest": None},
            "manifest must be omitted when unknown",
        ),
        (
            _with_placement(
                NOT_EVALUATED,
                0,
                undeployed("fixture-localization") | {"host": None},
            ),
            "host must be omitted when unknown",
        ),
        (
            _with_placement(
                SUCCEEDED, 1, _without(deployed("fixture-navigation"), "image_id")
            ),
            "image_id",
        ),
        (
            _with_placement(
                SUCCEEDED, 1, deployed("fixture-navigation") | {"phase": "Pending"}
            ),
            "phase",
        ),
        (
            _with_placement(
                SUCCEEDED, 1, deployed("fixture-navigation") | {"restart_count": 1}
            ),
            "restart_count",
        ),
        (
            _with_placement(
                SUCCEEDED,
                1,
                deployed("fixture-navigation") | {"detail": "CrashLoopBackOff"},
            ),
            "Extra inputs are not permitted",
        ),
        (
            _with_placement(
                SUCCEEDED,
                1,
                deployed("fixture-navigation")
                | {"image": image("fixture-localization")},
            ),
            "a placement image must be registry:5000/sdi/<service_id>@<digest>",
        ),
        (
            _with_placement(
                FAILED,
                1,
                CRASH | {"image": f"docker.io/sdi/fixture-navigation@{AMD64_DIGEST}"},
            ),
            "image",
        ),
        (
            _with_placement(
                SUCCEEDED, 1, deployed("fixture-navigation") | {"image_id": "nginx"}
            ),
            "image_id",
        ),
        (
            _with_placement(
                SUCCEEDED, 1, _without(deployed("fixture-navigation"), "host")
            ),
            "host",
        ),
        (
            _with_placement(
                NOT_EVALUATED,
                1,
                undeployed("fixture-navigation", "image-pull-backoff"),
            ),
            "reason",
        ),
        (
            _with_placement(SUCCEEDED, 1, deployed("fixture-localization")),
            "service_id values must be unique",
        ),
        (result("succeeded", []), "placements"),
        (SUCCEEDED | {"evidence_basis": "fixture"}, "evidence_basis"),
        (SUCCEEDED | {"cluster_kind": "devices"}, "cluster_kind"),
        (_without(SUCCEEDED, "cluster_kind"), "cluster_kind"),
        (
            SUCCEEDED | {"application_status": "not_evaluated"},
            "Extra inputs are not permitted",
        ),
    ],
    ids=[
        "succeeded-with-a-failed-placement",
        "failed-claimed-succeeded",
        "succeeded-claimed-not-evaluated",
        "succeeded-without-manifest",
        "failed-without-manifest",
        "not-evaluated-with-manifest",
        "gap-with-only-blocked-placements",
        "gap-beside-a-deployed-placement",
        "detail-not-ascii",
        "detail-over-256",
        "detail-with-quote",
        "detail-with-backslash",
        "detail-empty",
        "failed-without-detail",
        "null-node",
        "null-image-id",
        "null-manifest",
        "null-host",
        "deployed-without-image-id",
        "deployed-not-running",
        "deployed-restarted",
        "deployed-with-detail",
        "image-of-another-service",
        "image-outside-the-registry",
        "image-id-without-digest",
        "deployed-without-host",
        "reason-outside-the-closed-set",
        "duplicate-service",
        "no-placements",
        "fixture-basis",
        "devices-cluster",
        "no-cluster-kind",
        "fixture-application-status",
    ],
)
def test_cd_result_rejects_a_broken_rule(document: dict[str, Any], rule: str) -> None:
    with pytest.raises(ValidationError, match=rule):
        _validate(document)
