"""Public-boundary checks for assessing and ranking composition proposals."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests._proposals import COMPLETE

REPOSITORY_ROOT = Path(__file__).parents[2]
SERVICE_REPOSITORY = REPOSITORY_ROOT / "service-repository"
FAKE_DESCRIPTIONS = Path(__file__).parent / "data/service-descriptions"
PROFILE = REPOSITORY_ROOT / "profiles/s-04/waffle-jetson-arm64.yaml"
REQUIREMENTS = REPOSITORY_ROOT / "requirements/s-04/deliver-book-to-joe.md"


def _variant(
    *,
    drop: tuple[str, ...] = (),
    add: tuple[dict[str, str], ...] = (),
    hosts: dict[str, str | None] | None = None,
    coverage: dict[str, tuple[str, list[str]]] | None = None,
) -> dict[str, Any]:
    proposal = copy.deepcopy(COMPLETE)
    proposal["services"] = [
        service for service in proposal["services"] if service["service_id"] not in drop
    ] + [{"capability": "test-only", **service} for service in add]
    for service in proposal["services"]:
        if service["service_id"] in (hosts or {}):
            host = (hosts or {})[service["service_id"]]
            if host is None:
                del service["host"]
            else:
                service["host"] = host
    for entry in proposal["coverage"]:
        if entry["requirement_id"] in (coverage or {}):
            entry["status"], entry["services"] = (coverage or {})[
                entry["requirement_id"]
            ]
    return proposal


def _assess(
    tmp_path: Path,
    proposals: list[dict[str, Any]],
    profile_edit: tuple[str, str] = ("", ""),
) -> subprocess.CompletedProcess[str]:
    services = tmp_path / "services"
    if not services.exists():
        shutil.copytree(SERVICE_REPOSITORY, services)
        shutil.copytree(FAKE_DESCRIPTIONS, services / "fakes")
    profile = tmp_path / "profile.yaml"
    profile.write_text(PROFILE.read_text().replace(*profile_edit))
    proposals_path = tmp_path / "proposals.json"
    proposals_path.write_text(json.dumps({"proposals": proposals}))
    return subprocess.run(
        [
            "sdi-integration",
            "assess-proposals",
            "--service-repository",
            str(services),
            "--target-profile",
            str(profile),
            "--requirements-specification",
            str(REQUIREMENTS),
            "--proposals",
            str(proposals_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _assessment(
    tmp_path: Path,
    proposals: list[dict[str, Any]],
    profile_edit: tuple[str, str] = ("", ""),
) -> dict[str, Any]:
    completed = _assess(tmp_path, proposals, profile_edit)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


INSUFFICIENT = "no proposal is eligible"


def _kinds(proposal: dict[str, Any]) -> list[str]:
    """Defect kinds; every apt route also records a mutable-artifact finding."""
    return sorted(
        {
            finding["kind"]
            for finding in proposal["findings"]
            if finding["kind"] != "mutable-artifact"
        }
    )


WITHOUT_FACE_RECOG = _variant(
    drop=("face-recog",), coverage={"verify-recipient-face": ("missing", [])}
)


WITHOUT_CAMERA = _variant(
    drop=("v4l2-camera",),
    coverage={"verify-recipient-face": ("supported", ["face-recog"])},
)


@pytest.mark.parametrize(
    ("proposals", "ranks", "preferred", "tie"),
    [
        pytest.param(
            [
                _variant(
                    drop=("nav2-localization",),
                    add=(
                        {
                            "service_id": "fake-unmet-dependency",
                            "artifact_id": "jazzy-source",
                            "host": "orin",
                        },
                    ),
                ),
                COMPLETE,
            ],
            [(3, 2, "supported"), (5, 0, None)],
            "proposal-2",
            False,
            id="complete-beats-without-localization",
        ),
        pytest.param(
            [WITHOUT_FACE_RECOG, COMPLETE],
            [(4, 0, "supported"), (5, 0, None)],
            "proposal-2",
            False,
            id="supported-count-decides",
        ),
        pytest.param(
            [WITHOUT_FACE_RECOG, WITHOUT_CAMERA],
            [(4, 0, "supported"), (5, 1, None)],
            "proposal-2",
            False,
            id="coverage-beats-gaps",
        ),
        pytest.param(
            [WITHOUT_CAMERA, COMPLETE],
            [(5, 1, "gaps"), (5, 0, None)],
            "proposal-2",
            False,
            id="gaps-decide-equal-coverage",
        ),
        pytest.param(
            [COMPLETE, _variant(hosts={"nav2-navigation": "waffle"})],
            [(5, 0, None), (5, 0, "model-order")],
            "proposal-1",
            True,
            id="tie-broken-by-model-order",
        ),
    ],
)
def test_prefers_the_most_supported_proposal_then_fewest_gaps(
    tmp_path: Path,
    proposals: list[dict[str, Any]],
    ranks: list[tuple[int, int, str | None]],
    preferred: str,
    *,
    tie: bool,
) -> None:
    assessment = _assessment(tmp_path, proposals)

    assert [
        (
            proposal["score"]["supported"],
            proposal["score"]["gaps"],
            proposal["decided_by"],
        )
        for proposal in assessment["proposals"]
    ] == ranks
    assert assessment["preferred"] == preferred
    assert assessment["tie_broken_by_model_order"] is tie
    assert assessment["ranking"]["order"][0] == preferred
    assert "none available" in assessment["ranking"]["performance_evidence"]
    assert assessment["proposals"][0]["proposal"] == proposals[0]


@pytest.mark.parametrize(
    ("proposal", "profile_edit", "kinds", "score", "placements"),
    [
        pytest.param(
            _variant(
                drop=("v4l2-camera",),
                add=(
                    {
                        "service_id": "fake-compressed-camera",
                        "artifact_id": "jazzy-source",
                        "host": "waffle",
                    },
                ),
                coverage={
                    "verify-recipient-face": (
                        "supported",
                        ["face-recog", "fake-compressed-camera"],
                    )
                },
            ),
            ("", ""),
            ["binding-rejected", "missing-provider"],
            (1, 0),
            {},
            id="type-mismatch",
        ),
        pytest.param(
            _variant(
                drop=("v4l2-camera",),
                add=(
                    {
                        "service_id": "fake-best-effort-camera",
                        "artifact_id": "jazzy-source",
                        "host": "waffle",
                    },
                ),
                coverage={
                    "verify-recipient-face": (
                        "supported",
                        ["face-recog", "fake-best-effort-camera"],
                    )
                },
            ),
            ("", ""),
            ["binding-rejected", "missing-provider"],
            (1, 0),
            {},
            id="qos-mismatch",
        ),
        pytest.param(
            _variant(
                add=(
                    {
                        "service_id": "fake-unverified-endpoints",
                        "artifact_id": "jazzy-source",
                        "host": "orin",
                    },
                )
            ),
            ("", ""),
            ["endpoints-unverified"],
            (0, 0),
            {},
            id="unverified-endpoints",
        ),
        pytest.param(
            _variant(
                add=(
                    {
                        "service_id": "hri-face-detect",
                        "artifact_id": "humble-source",
                        "host": "orin",
                    },
                )
            ),
            ("", ""),
            ["placement-rejected"],
            (0, 0),
            {"hri-face-detect": "rejected"},
            id="baseline-mismatch",
        ),
        pytest.param(
            _variant(
                add=(
                    {
                        "service_id": "fake-missing-artifact",
                        "artifact_id": "invented",
                        "host": "orin",
                    },
                )
            ),
            ("", ""),
            ["missing-artifact"],
            (1, 0),
            {"fake-missing-artifact": "unresolved"},
            id="artifact-id-without-artifacts",
        ),
        pytest.param(
            {
                **_variant(hosts={"whisper-server": "waffle"}),
                "services": [
                    *_variant(drop=("whisper-server",))["services"],
                    {
                        "service_id": "whisper-server",
                        "host": "waffle",
                        "capability": "speech to text",
                    },
                ],
            },
            ("", ""),
            ["coverage-downgraded", "placement-rejected"],
            (0, 3),
            {"whisper-server": "rejected"},
            id="removed-service-keeps-no-gaps",
        ),
        pytest.param(
            COMPLETE,
            ("    devices: [opencr, lidar, camera, microphone]\n", ""),
            ["unresolved-placement"],
            (3, 0),
            dict.fromkeys(
                ["turtlebot3-bringup", "v4l2-camera", "whisper-audio-listener"],
                "unresolved",
            ),
            id="host-without-device-list",
        ),
        pytest.param(
            _variant(hosts={"face-recog": "jetson-nano", "v4l2-camera": None}),
            ("", ""),
            ["unresolved-placement"],
            (2, 0),
            dict.fromkeys(["face-recog", "v4l2-camera"], "unresolved"),
            id="unknown-and-omitted-host",
        ),
        pytest.param(
            COMPLETE,
            ("connections:\n  - [waffle, orin]\n", ""),
            ["unresolved-connection"],
            (1, 0),
            {},
            id="no-connection",
        ),
    ],
)
def test_records_findings_and_their_gaps(  # noqa: PLR0913, PLR0917
    tmp_path: Path,
    proposal: dict[str, Any],
    profile_edit: tuple[str, str],
    kinds: list[str],
    score: tuple[int, int],
    placements: dict[str, str],
) -> None:
    assessment = _assessment(tmp_path, [proposal], profile_edit)

    [assessed] = assessment["proposals"]
    assert _kinds(assessed) == kinds
    assert (assessed["score"]["gaps"], assessed["score"]["missing"]) == score
    assert {
        service["service_id"]: service["placement"]
        for service in assessed["services"]
        if service["placement"] != "resolved"
    } == placements
    assert assessment["preferred"] == "proposal-1"


def test_records_each_chosen_route_and_flags_mutable_ones(tmp_path: Path) -> None:
    images = _variant(
        add=(
            {"service_id": "fake-tagged-image", "artifact_id": "image", "host": "orin"},
            {"service_id": "fake-digest-image", "artifact_id": "image", "host": "orin"},
        )
    )

    baseline, assessed = _assessment(tmp_path, [COMPLETE, images])["proposals"]

    routes = {
        service["service_id"]: service["route"] for service in assessed["services"]
    }
    assert routes["fake-tagged-image"] == {
        "kind": "image",
        "reference": "example.invalid/sdi-test-fakes:jazzy",
    }
    assert routes["face-recog"] == {
        "kind": "source",
        "repository": "https://github.com/elpidiovaldez/face_recog",
        "revision": "49604f569e1ef0c6283c2a8369f580167087b874",
    }
    assert routes["turtlebot3-bringup"]["kind"] == "apt"
    mutable = [
        finding
        for finding in assessed["findings"]
        if finding["kind"] == "mutable-artifact"
    ]
    assert {finding["service_id"] for finding in mutable} == {
        "turtlebot3-bringup",
        "nav2-localization",
        "nav2-navigation",
        "v4l2-camera",
        "fake-tagged-image",
    }
    assert not any(finding["gap"] for finding in mutable)
    assert routes["fake-digest-image"]["kind"] == "image"
    assert "version patterns" in next(
        finding["message"]
        for finding in mutable
        if finding["service_id"] == "turtlebot3-bringup"
    )
    assert assessed["score"] == baseline["score"]


@pytest.mark.parametrize(
    ("proposal", "reason"),
    [
        pytest.param(
            _variant(add=({"service_id": "invented", "host": "orin"},)),
            INSUFFICIENT,
            id="unknown-service",
        ),
        pytest.param(
            _variant(
                drop=("face-recog",),
                add=(
                    {
                        "service_id": "face-recog",
                        "artifact_id": "invented",
                        "host": "orin",
                    },
                ),
            ),
            INSUFFICIENT,
            id="unknown-artifact",
        ),
        pytest.param(
            _variant(
                add=(
                    {
                        "service_id": "face-recog",
                        "artifact_id": "jazzy-source",
                        "host": "waffle",
                    },
                )
            ),
            INSUFFICIENT,
            id="duplicate-service",
        ),
        pytest.param(
            {**COMPLETE, "coverage": COMPLETE["coverage"][1:]},
            INSUFFICIENT,
            id="coverage-mismatch",
        ),
        pytest.param(
            _variant(coverage={"transfer-book": ("missing", ["face-recog"])}),
            INSUFFICIENT,
            id="negative-status-cites-services",
        ),
        pytest.param(
            {
                **COMPLETE,
                "services": [
                    {
                        "service_id": "whisper-server",
                        "artifact_id": "jazzy-cuda-source",
                        "host": "waffle",
                        "capability": "speech to text",
                    }
                ],
            },
            "every proposal lost all of its services to placement removals",
            id="no-service-remains",
        ),
    ],
)
def test_reports_no_preferred_proposal_when_none_is_eligible(
    tmp_path: Path, proposal: dict[str, Any], reason: str
) -> None:
    assessment = _assessment(tmp_path, [proposal])

    [assessed] = assessment["proposals"]
    assert assessed["eligible"] is False
    assert assessed["score"] is None
    assert "ineligible" in _kinds(assessed)
    assert assessment["preferred"] is None
    assert assessment["reason"] == reason


def test_rejects_proposals_outside_the_schema(tmp_path: Path) -> None:
    completed = _assess(tmp_path, [])

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "proposals.json: contract validation failed" in completed.stderr
