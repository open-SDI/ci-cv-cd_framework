"""CV decides every not-evaluated cause before it opens a docker client."""

from __future__ import annotations

import subprocess
import sys
from functools import partial
from typing import TYPE_CHECKING, Any

import pytest

from sdi_pipeline_integration._adapter_kit import serve
from sdi_pipeline_integration._cv_adapter import validate
from sdi_pipeline_integration._domain_contracts import UnevaluatedService
from tests._cv_builders import (
    ARM64_DIGEST,
    INDEX_DIGEST,
    POSE,
    STRING,
    amd64_digest,
)
from tests._cv_stage import CvRun, CvStage, description, refusing_engine_url

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

ENGINE_URL = refusing_engine_url()
HEALTHY = {
    "nav2-localization": description(
        "nav2-localization", {"/amcl_pose": POSE, "/tf": "tf2_msgs/msg/TFMessage"}, []
    ),
    "vosk-speech": description("vosk-speech", {"/speech/text": STRING}),
}


def _built(service_id: str, *architectures: str) -> dict[str, Any]:
    digests = {"amd64": amd64_digest(service_id), "arm64": ARM64_DIGEST}
    return {
        "service_id": service_id,
        "status": "built",
        "repository": f"sdi/{service_id}",
        "digest": INDEX_DIGEST,
        "platforms": [
            {"architecture": name, "digest": digests[name]}
            for name in architectures or ("amd64", "arm64")
        ],
    }


def _failed(service_id: str, reason: str) -> dict[str, Any]:
    return {
        "service_id": service_id,
        "status": "failed",
        "reason": reason,
        "detail": "recorded by a test",
    }


def _run(
    tmp_path: Path,
    extra: dict[str, dict[str, Any]],
    images: Mapping[str, dict[str, Any] | None],
) -> CvRun:
    services = extra | HEALTHY
    records = [
        images[service_id] if service_id in images else _built(service_id)
        for service_id in services
    ]
    stage = CvStage(tmp_path)
    stage.write(services, [item for item in records if item is not None])
    return stage.run(serve("cv", partial(validate, engine_url=ENGINE_URL)))


def _causes(run: CvRun) -> dict[str, tuple[str, str | None]]:
    assert run.evidence is not None
    assert run.evidence.mission is None
    assert run.evidence.domain_outcome == "not_evaluated"
    return {
        item.service_id: (item.reason, item.detail)
        for item in run.evidence.services
        if isinstance(item, UnevaluatedService)
    }


type Case = tuple[
    dict[str, dict[str, Any]], Mapping[str, dict[str, Any] | None], tuple[str, str]
]
CASES: dict[str, Case] = {
    "build-failed": (
        {"face": description("face", {"/name": STRING})},
        {"face": _failed("face", "build-failed")},
        ("image-unavailable", "image-build recorded build-failed"),
    ),
    "unresolved-placement": (
        {"face": description("face", {"/name": STRING})},
        {"face": _failed("face", "unresolved-placement")},
        ("image-unavailable", "image-build recorded unresolved-placement"),
    ),
    "no-image-record": (
        {"face": description("face", {"/name": STRING})},
        {"face": None},
        ("image-unavailable", "image-build recorded no image"),
    ),
    "no-amd64": (
        {"face": description("face", {"/name": STRING})},
        {"face": _built("face", "arm64")},
        ("no-amd64", "image-build pushed no amd64 image"),
    ),
    "gpu": (
        {"face": description("face", {"/name": STRING}, ["nvidia-gpu"])},
        {},
        ("needs-gpu-or-device", "declares nvidia-gpu"),
    ),
    "unsimulated-device": (
        {"face": description("face", {"/name": STRING}, ["camera", "sonar"])},
        {},
        ("needs-gpu-or-device", "declares sonar"),
    ),
    "no-publishes": (
        {"face": description("face")},
        {},
        ("no-counting-output", "declares no published topic"),
    ),
    "only-action-servers": (
        {"face": description("face") | {"action_servers": {"/find": {"type": "a"}}}},
        {},
        ("no-counting-output", "declares no published topic"),
    ),
    "outputs-in-the-constant": (
        {"face": description("face", {"/tf": "t", "/odom": "o"})},
        {},
        ("no-counting-output", "all outputs in the topic constant"),
    ),
    "outputs-shared": (
        {"face": description("face", {"/speech/text": STRING, "/tf": "t"})},
        {},
        ("no-counting-output", "all outputs shared with vosk-speech"),
    ),
}


@pytest.mark.parametrize("case", CASES)
def test_cv_a_declaration_cause_leaves_the_composition_not_evaluated(
    tmp_path: Path, case: str
) -> None:
    extra, images, cause = CASES[case]

    run = _run(tmp_path, extra, images)

    assert run.response.execution_conclusion == "succeeded"
    assert run.response.domain_outcome == "not_evaluated"
    assert run.response.reason is not None
    assert run.response.reason.model_dump() == {
        "category": "dependency",
        "code": "sdi.cv.not-evaluated",
        "summary": f"face was not evaluated: {cause[0]}.",
    }
    expected = {"face": cause} | dict.fromkeys(HEALTHY, ("blocked", None))
    if case == "outputs-shared":
        expected["vosk-speech"] = ("no-counting-output", "all outputs shared with face")
    assert _causes(run) == expected
    assert "face: not_evaluated" in run.diagnostic


def test_cv_each_service_reports_its_first_cause_in_order(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        {
            "gpu-unbuilt": description("gpu-unbuilt", {"/a": STRING}, ["nvidia-gpu"]),
            "silent-arm64": description("silent-arm64"),
            "gpu-silent": description("gpu-silent", None, ["nvidia-gpu"]),
        },
        {
            "gpu-unbuilt": _failed("gpu-unbuilt", "build-failed"),
            "silent-arm64": _built("silent-arm64", "arm64"),
        },
    )

    assert {name: reason for name, (reason, _detail) in _causes(run).items()} == {
        "nav2-localization": "blocked",
        "vosk-speech": "blocked",
        "gpu-unbuilt": "image-unavailable",
        "silent-arm64": "no-amd64",
        "gpu-silent": "needs-gpu-or-device",
    }


def test_cv_the_summary_names_the_first_cause_in_spec_order(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        {
            "silent": description("silent"),
            "gpu": description("gpu", {"/b": STRING}, ["nvidia-gpu"]),
            "arm64": description("arm64", {"/c": STRING}),
        },
        {"arm64": _built("arm64", "arm64")},
    )

    assert run.response.reason is not None
    assert run.response.reason.summary == "arm64 was not evaluated: no-amd64."


def test_cv_loosened_services_need_no_counting_output(tmp_path: Path) -> None:
    run = _run(
        tmp_path,
        {
            "camera": description(
                "camera", {"/image": "sensor_msgs/msg/Image"}, ["camera"]
            ),
            "bringup": description("bringup", None, ["opencr", "lidar"]),
            "gpu": description("gpu", {"/b": STRING}, ["nvidia-gpu"]),
        },
        {},
    )

    causes = _causes(run)
    assert causes["camera"] == ("blocked", None)
    assert causes["bringup"] == ("blocked", None)
    assert causes["gpu"][0] == "needs-gpu-or-device"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"blueprint_repository_digest": "f" * 64}, "input-mismatch"),
        ({"target_id": "xycar"}, "unsupported-robot"),
    ],
    ids=["input-mismatch", "unsupported-robot"],
)
def test_cv_an_input_cv_cannot_use_is_an_input_error(
    tmp_path: Path, changes: dict[str, Any], code: str
) -> None:
    stage = CvStage(tmp_path)
    stage.write(HEALTHY, [_built(name) for name in HEALTHY], **changes)

    run = stage.run(serve("cv", partial(validate, engine_url=ENGINE_URL)))

    assert run.response.execution_conclusion == "failed"
    assert run.response.domain_outcome == "not_evaluated"
    assert run.response.reason is not None
    assert (run.response.reason.code, run.response.reason.category) == (
        f"sdi.cv.{code}",
        "input",
    )
    assert run.response.produced_outputs == []
    assert run.evidence is None


NO_DOCKER = """
import sys
from functools import partial

from sdi_pipeline_integration._adapter_kit import serve
from sdi_pipeline_integration._cv_adapter import validate

code = serve("cv", partial(validate, engine_url=sys.argv[1]))(sys.argv[2:])
assert "docker" not in sys.modules, "the adapter imported docker"
sys.exit(code)
"""


def test_cv_a_composition_cv_cannot_evaluate_never_loads_docker(
    tmp_path: Path,
) -> None:
    stage = CvStage(tmp_path)
    stage.write(
        HEALTHY | {"face": description("face", {"/name": STRING}, ["nvidia-gpu"])},
        [_built(name) for name in [*HEALTHY, "face"]],
    )

    completed = subprocess.run(
        [sys.executable, "-c", NO_DOCKER, ENGINE_URL, *stage.argv()],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    run = stage.published(completed.returncode)
    assert run.response.domain_outcome == "not_evaluated"
    assert _causes(run)["face"][0] == "needs-gpu-or-device"
