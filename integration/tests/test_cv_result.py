"""The orchestrator's result file as the CV run loop parses it."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import ValidationError

from sdi_pipeline_integration._cv_result import REQUIREMENTS, ScenarioResult

if TYPE_CHECKING:
    from collections.abc import Sequence

WAITS = {
    "accept-spoken-request": "/speech/text",
    "navigate-to-rendezvous": "/navigate_to_pose",
    "verify-recipient-face": "/face_recognition/name",
    "acknowledge-receipt": "/speech/text",
    "return-to-origin": "/navigate_to_pose",
}
PASS = (
    "scripted",
    "passed",
    "scripted",
    "passed",
    "passed",
    "scripted",
    "passed",
    "passed",
)
NO_FACE = (
    "scripted",
    "passed",
    "scripted",
    "passed",
    "timed_out",
    *("not_reached",) * 3,
)


def _step(requirement_id: str, status: str) -> dict[str, object]:
    step: dict[str, object] = {"requirement_id": requirement_id, "status": status}
    if requirement_id in WAITS:
        step["waited_on"] = WAITS[requirement_id]
    if status in {"passed", "failed", "timed_out"}:
        step["elapsed_s"] = 12.5
    return step


def _result(
    statuses: Sequence[str] = PASS, setup: dict[str, object] | None = None
) -> dict[str, object]:
    return {
        "unloadable_types": [],
        "counts": {"/amcl_pose": 412, "/speech/text": 2},
        "mission": {
            "setup": setup or {"status": "passed", "elapsed_s": 40.1},
            "steps": [
                _step(requirement_id, status)
                for requirement_id, status in zip(REQUIREMENTS, statuses, strict=True)
            ],
        },
        "graph": [
            {
                "topic": "/speech/text",
                "types": ["std_msgs/msg/String"],
                "publishers": ["/vosk_speech"],
            }
        ],
    }


def _parse(document: dict[str, object]) -> ScenarioResult:
    return ScenarioResult.model_validate_json(json.dumps(document))


def test_cv_result_a_passing_run_completes_the_mission() -> None:
    result = _parse(_result())

    assert result.mission is not None
    assert result.mission.completed
    assert [step.status for step in result.mission.steps] == list(PASS)


def test_cv_result_a_face_timeout_ends_the_mission() -> None:
    result = _parse(_result(NO_FACE))

    assert result.mission is not None
    assert not result.mission.completed


def test_cv_result_a_setup_timeout_names_its_wait_and_reaches_no_step() -> None:
    setup: dict[str, object] = {
        "status": "timed_out",
        "elapsed_s": 300.0,
        "waited_on": "x",
    }
    result = _parse(_result(("not_reached",) * 8, setup))

    assert result.mission is not None
    assert result.mission.setup.waited_on == "x"


def test_cv_result_unloadable_types_carry_no_counts_and_no_mission() -> None:
    result = _parse(
        {
            "unloadable_types": [{"topic": "/x", "type": "pkg/msg/T"}],
            "counts": {},
            "graph": [],
        }
    )

    assert result.mission is None


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(_result(PASS[::-1]), id="steps out of order"),
        pytest.param(_result(("scripted",) * 8), id="a gated step scripted"),
        pytest.param(_result(("passed",) * 8), id="a scripted step passed"),
        pytest.param(
            _result((*NO_FACE[:5], "scripted", "passed", "passed")),
            id="a step after a timeout reached",
        ),
        pytest.param(
            _result(PASS[:3] + ("not_reached",) * 5),
            id="a step not reached before the end",
        ),
        pytest.param(
            _result(
                PASS, {"status": "timed_out", "elapsed_s": 300.0, "waited_on": "x"}
            ),
            id="setup timed out but a step ran",
        ),
        pytest.param(
            _result(("not_reached",) * 8, {"status": "timed_out", "elapsed_s": 300.0}),
            id="setup timed out without its wait",
        ),
        pytest.param(
            _result(PASS, {"status": "passed", "elapsed_s": 1.0, "waited_on": "x"}),
            id="setup passed with a wait",
        ),
    ],
)
def test_cv_result_refuses_a_mission_the_orchestrator_cannot_produce(
    document: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _parse(document)


def _edit_step(index: int, **changes: object) -> dict[str, object]:
    document = _result()
    mission = document["mission"]
    assert isinstance(mission, dict)
    steps = cast("list[dict[str, object]]", mission["steps"])
    steps[index] = {**steps[index], **changes}
    return document


@pytest.mark.parametrize(
    "document",
    [
        _edit_step(1, elapsed_s=None),
        _edit_step(0, elapsed_s=1.0),
        _edit_step(0, waited_on="/x"),
        _edit_step(1, waited_on=None),
        _edit_step(1, unexpected=True),
    ],
)
def test_cv_result_refuses_step_fields_that_do_not_fit_the_status(
    document: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _parse(document)


def test_cv_result_requires_a_mission_when_every_type_loaded() -> None:
    document = _result()
    del document["mission"]

    with pytest.raises(ValidationError):
        _parse(document)


def test_cv_result_refuses_counts_beside_unloadable_types() -> None:
    document = _result()
    document["unloadable_types"] = [{"topic": "/x", "type": "pkg/msg/T"}]

    with pytest.raises(ValidationError):
        _parse(document)
