"""CV's diagnostic.txt: its layout, its 16 KiB budget and what it cuts first."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from sdi_pipeline_integration._adapter_kit import Diagnostic
from sdi_pipeline_integration._cv_diagnostic import (
    GRAPH_MAX_BYTES,
    describe,
    water_fill,
)
from sdi_pipeline_integration._cv_result import GraphTopic
from sdi_pipeline_integration._cv_verdict import CvError, Judgement, assess
from sdi_pipeline_integration._stage_contracts import contains_sensitive_material
from sdi_pipeline_integration._stage_registry import STAGES
from tests._cv_builders import (
    POSE,
    STRING,
    TWIST,
    composed,
    errored,
    exited,
    finished,
    runner,
)
from tests._cv_recordings import NAMES, recorded

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sdi_pipeline_integration._cv_run import ContainerState, FailedToStart

MAX_BYTES = STAGES["cv"].grant.diagnostic_max_bytes
SERVICES = (
    composed("nav2-localization", {"/amcl_pose": POSE}),
    composed("nav2-navigation", {"/cmd_vel": TWIST}),
    composed("vosk-speech", {"/speech/text": STRING}),
    composed("opencv-face-recognition", {"/face_recognition/name": STRING}),
)
STEP = re.compile(r"^step (?P<id>[a-z-]+): passed (?P<on>\S+) (?P<elapsed>[0-9.]+) s$")


def _render(result: Judgement | CvError) -> tuple[list[str], bool]:
    diagnostic = Diagnostic("sdi.cv", MAX_BYTES)
    describe(diagnostic, result)
    content, cut = diagnostic.render(result.outcome)
    assert len(content) <= MAX_BYTES
    text = content.decode()
    assert not contains_sensitive_material(text)
    return text.splitlines(), cut


def _recorded(
    name: str,
    states: Mapping[str, ContainerState | FailedToStart] | None = None,
    graph: Sequence[GraphTopic] | None = None,
) -> Judgement:
    result = recorded(name)
    services = SERVICES if name != "no-face" else SERVICES[:3]
    judgement = assess(
        services,
        runner(
            finished(
                counts=result.counts,
                scenario_mission=result.mission,
                states=states,
                graph=result.graph if graph is None else graph,
            )
        ),
    )
    assert isinstance(judgement, Judgement)
    return judgement


@pytest.mark.parametrize("name", NAMES)
def test_cv_diagnostic_lays_out_each_recorded_run(name: str) -> None:
    lines, cut = _render(_recorded(name))

    assert not cut
    assert lines[0] == (
        "outcome: sdi.cv.succeeded" if name != "no-face" else "outcome: sdi.cv.failed"
    )
    assert [line.split(":")[0] for line in lines if line.startswith("phase ")] == [
        "phase leftover-cleanup",
        "phase sim-build",
        "phase pulls",
        "phase start",
        "phase orchestrator-wait",
    ]
    mission = recorded(name).mission
    assert mission is not None
    assert f"setup: passed {mission.setup.elapsed_s} s" in lines
    passed = {
        match["id"]: (match["on"], float(match["elapsed"]))
        for line in lines
        if (match := STEP.match(line))
    }
    assert passed == {
        item.requirement_id: (item.waited_on, item.elapsed_s)
        for item in mission.steps
        if item.status == "passed"
    }
    assert "step secure-book: scripted" in lines
    assert "service nav2-localization: participated" in lines
    assert "count /cmd_vel geometry_msgs/msg/TwistStamped: " in "\n".join(lines)
    assert "topic-constant: match" in lines
    graph = [line for line in lines if line.startswith("graph ")]
    assert any(
        line.startswith("graph /s04_bridge: /camera_info /clock") for line in graph
    )
    assert re.fullmatch(r"graph [0-9]+ nodes: /rosout", graph[-1])


def test_cv_diagnostic_tails_the_services_that_did_not_participate() -> None:
    log = [f"line {index:03} {'x' * 200}" for index in range(400)]

    lines, cut = _render(_recorded("no-face", {"vosk-speech": exited(137, *log)}))

    assert cut
    assert (
        lines[1]
        == "summary: vosk-speech did not participate: container-exited (exit 137)."
    )
    assert "service vosk-speech: did_not_participate container-exited exit 137" in lines
    tail = lines[lines.index("--- vosk-speech log tail") + 1 :]
    assert tail[0] != log[0]
    assert log[-1] in tail
    assert "--- orchestrator log tail" in tail
    assert any(line.endswith(" nodes: /rosout") for line in lines)
    assert "topic-constant: match" in lines


def test_cv_diagnostic_keeps_short_tails_whole_and_splits_the_rest() -> None:
    long_log = [f"line {index:03} {'y' * 200}" for index in range(400)]
    states = {
        "vosk-speech": exited(1, "speech died"),
        "nav2-navigation": exited(2, *long_log),
        "nav2-localization": exited(3, *long_log),
    }

    lines, cut = _render(_recorded("waffle-pass", states))

    assert cut
    localization = lines.index("--- nav2-localization log tail")
    navigation = lines.index("--- nav2-navigation log tail")
    speech = lines.index("--- vosk-speech log tail")
    assert lines[speech + 1] == "speech died"
    assert lines[navigation - 1] == long_log[-1]
    assert lines[speech - 1] == long_log[-1]
    assert abs((navigation - localization) - (speech - navigation)) <= 1


@pytest.mark.parametrize(
    ("room", "sizes", "shares"),
    [
        (10, [2, 9, 9], [2, 4, 4]),
        (100, [10, 20], [10, 20]),
        (9, [9, 2, 9], [3, 2, 4]),
        (0, [5, 5], [0, 0]),
        (-3, [5], [0]),
        (7, [], []),
    ],
)
def test_cv_water_fill_shares_room_smallest_first(
    room: int, sizes: list[int], shares: list[int]
) -> None:
    assert water_fill(room, sizes) == shares
    assert sum(shares) <= max(room, 0)


def test_cv_the_graph_survives_a_sensitive_log_line() -> None:
    log = (
        "connecting to http://10.0.0.7:8080/models",
        "password=hunter2",
        "giving up",
    )

    lines, _cut = _render(_recorded("no-face", {"vosk-speech": exited(1, *log)}))

    tail = lines[lines.index("--- vosk-speech log tail") + 1 :]
    assert tail[:2] == ["connecting to <url>", "giving up"]
    assert not any("hunter2" in line for line in lines)
    assert "topic-constant: match" in lines
    assert any(line.startswith("graph /s04_bridge: ") for line in lines)


def test_cv_the_graph_stops_at_its_budget() -> None:
    graph = [
        GraphTopic(
            topic=f"/topic_{index}",
            types=["t"],
            publishers=[f"/node_{index:04}" + "n" * 60],
        )
        for index in range(400)
    ]

    lines, cut = _render(_recorded("waffle-pass", graph=graph))

    assert cut
    graph_lines = [line for line in lines if line.startswith("graph /node_")]
    assert sum(len(line) + 1 for line in graph_lines) <= GRAPH_MAX_BYTES
    assert lines[-1] == f"graph: {400 - len(graph_lines)} more publisher sets cut"
    assert any(
        line.startswith("topic-constant: missing /audio_listener/audio")
        for line in lines
    )


def test_cv_a_cv_error_names_its_error_and_tails_sim_and_orchestrator() -> None:
    error = assess(SERVICES, runner(errored("orchestrator-timeout")))
    assert isinstance(error, CvError)

    lines, _cut = _render(error)

    assert lines[:3] == [
        "outcome: sdi.cv.orchestrator-timeout",
        "summary: The orchestrator did not finish within the work limit.",
        "error: orchestrator-timeout: orchestrator-timeout raised by a test",
    ]
    assert "phase orchestrator-wait: 60.1 s" in lines
    assert lines[lines.index("--- sim log tail") + 1] == "gz sim crashed"
    assert lines[lines.index("--- orchestrator log tail") + 1] == "waiting for setup"


def test_cv_a_not_evaluated_diagnostic_names_each_cause() -> None:
    gpu = composed("whisper-server", devices=["nvidia-gpu"])
    judgement = assess((SERVICES[0], gpu), runner(errored("no-result")))
    assert isinstance(judgement, Judgement)

    lines, cut = _render(judgement)

    assert not cut
    assert lines == [
        "outcome: sdi.cv.not-evaluated",
        "summary: whisper-server was not evaluated: needs-gpu-or-device.",
        "service nav2-localization: not_evaluated blocked",
        "service whisper-server: not_evaluated needs-gpu-or-device declares nvidia-gpu",
    ]
