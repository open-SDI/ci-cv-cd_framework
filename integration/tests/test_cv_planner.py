"""The CV run loop's pure planner: sim-time commands, run options, sim tag."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from sdi_pipeline_integration import _cv_run as run
from sdi_pipeline_integration._cv_run import (
    CountingOutput,
    Placement,
    RunRequest,
    plan,
    sim_image_tag,
    sim_time_command,
)

if TYPE_CHECKING:
    from pathlib import Path

EXECUTION_ID = "0b3c2f4e-8d1a-4c5e-9f00-123456789abc"
DIGEST = "sha256:" + "a" * 64


def _request() -> RunRequest:
    return RunRequest(
        execution_id=EXECUTION_ID,
        robot="burger_cam",
        placements=(
            Placement("nav2-navigation", DIGEST),
            Placement("vosk-speech", DIGEST),
        ),
        counting=(
            CountingOutput(
                topic="/speech/text", type="std_msgs/msg/String", service_id="vosk"
            ),
        ),
        deadline_monotonic=0.0,
    )


def test_cv_planner_appends_sim_time_last_to_ros2_launch() -> None:
    command = ["ros2", "launch", "/opt/x/launch.py", "use_sim_time:=false"]

    assert sim_time_command(command) == (*command, "use_sim_time:=true")


def test_cv_planner_appends_a_ros_args_block_to_ros2_run() -> None:
    assert sim_time_command(["ros2", "run", "vosk_speech", "vosk_speech"]) == (
        "ros2",
        "run",
        "vosk_speech",
        "vosk_speech",
        "--ros-args",
        "-p",
        "use_sim_time:=true",
    )


def test_cv_planner_appends_a_second_ros_args_block() -> None:
    command = ["ros2", "run", "v4l2_camera", "v4l2_camera_node"]
    command += ["--ros-args", "-r", "image_raw:=image"]

    assert sim_time_command(command) == (
        *command,
        "--ros-args",
        "-p",
        "use_sim_time:=true",
    )


@pytest.mark.parametrize(
    "command",
    [
        [],
        ["ros2", "launch"],
        ["ros2", "run", "only_a_package"],
        ["ros2", "topic", "echo", "/x"],
        ["/bin/sh", "-c", "ros2 run pkg node"],
        ["python3", "node.py"],
    ],
)
def test_cv_planner_refuses_any_other_command_shape(command: list[str]) -> None:
    assert sim_time_command(command) is None


def test_cv_planner_asks_the_sim_to_record_only_when_the_run_keeps_a_recording() -> (
    None
):
    commands = {
        "nav2-navigation": ("ros2", "launch", "x", "use_sim_time:=true"),
        "vosk-speech": ("ros2", "run", "a", "b"),
    }
    recorded = plan(
        replace(_request(), recording_max_bytes=1024), "sdi-cv-sim:tag", commands
    )
    unrecorded = plan(_request(), "sdi-cv-sim:tag", commands)

    assert recorded.sim.get("environment", {}).get("CV_RECORDING") == "1"
    assert unrecorded.sim.get("environment", {}).get("CV_RECORDING") == "0"
    assert recorded.orchestrator == unrecorded.orchestrator
    assert recorded.placements == unrecorded.placements


def test_cv_planner_joins_every_container_to_the_sims_namespaces() -> None:
    containers = plan(
        _request(),
        "sdi-cv-sim:tag",
        {
            "nav2-navigation": ("ros2", "launch", "x", "use_sim_time:=true"),
            "vosk-speech": ("ros2", "run", "a", "b"),
        },
    )

    sim: dict[str, object] = dict(containers.sim)
    assert sim["name"] == f"cv-{EXECUTION_ID}-sim"
    assert sim["image"] == "sdi-cv-sim:tag"
    assert sim["ipc_mode"] == "shareable"
    assert sim["shm_size"] == "1g"
    assert "network_mode" not in sim
    assert sim["environment"] == {"CV_ROBOT": "burger_cam", "CV_RECORDING": "0"}
    joined: list[dict[str, object]] = [
        dict(options)
        for options in [*containers.placements.values(), containers.orchestrator]
    ]
    for options in joined:
        assert options["network_mode"] == f"container:{sim['name']}"
        assert options["ipc_mode"] == f"container:{sim['name']}"
        assert "shm_size" not in options
    for options in [sim, *joined]:
        assert options["labels"] == {"org.sdi.cv-execution": EXECUTION_ID}
        assert options["detach"] is True
        assert set(options) <= {
            "image",
            "command",
            "name",
            "labels",
            "environment",
            "network_mode",
            "ipc_mode",
            "shm_size",
            "detach",
        }


def test_cv_planner_runs_placements_by_digest_with_the_rewritten_command() -> None:
    containers = plan(
        _request(),
        "sdi-cv-sim:tag",
        {
            "nav2-navigation": ("ros2", "launch", "x", "use_sim_time:=true"),
            "vosk-speech": ("ros2", "run", "a", "b"),
        },
    )

    navigation: dict[str, object] = dict(containers.placements["nav2-navigation"])
    assert navigation["image"] == f"registry:5000/sdi/nav2-navigation@{DIGEST}"
    assert navigation["command"] == ["ros2", "launch", "x", "use_sim_time:=true"]
    assert "environment" not in navigation
    assert list(containers.placements) == ["nav2-navigation", "vosk-speech"]


def test_cv_planner_hands_the_orchestrator_its_counting_list() -> None:
    commands = {"nav2-navigation": ("a",), "vosk-speech": ("b",)}
    orchestrator = plan(_request(), "sdi-cv-sim:tag", commands).orchestrator

    assert orchestrator.get("image") == "sdi-cv-sim:tag"
    assert orchestrator.get("command") == ["cv-orchestrator"]
    counting = orchestrator.get("environment", {})["CV_COUNTING"]
    assert " " not in counting
    assert json.loads(counting) == [
        {"topic": "/speech/text", "type": "std_msgs/msg/String", "service_id": "vosk"}
    ]


def _context(tmp_path: Path) -> Path:
    context = tmp_path / "context"
    shutil.copytree(run.SIM_CONTEXT, context)
    return context


def test_cv_sim_tag_follows_the_context_content(tmp_path: Path) -> None:
    context = _context(tmp_path)
    tag = sim_image_tag(context)

    assert tag == sim_image_tag(run.SIM_CONTEXT)
    assert tag.startswith("sdi-cv-sim:")
    (context / "scenario.yaml").write_bytes(
        (context / "scenario.yaml").read_bytes() + b"\n"
    )
    assert sim_image_tag(context) != tag


def test_cv_sim_tag_follows_added_and_renamed_files(tmp_path: Path) -> None:
    context = _context(tmp_path)
    tag = sim_image_tag(context)

    (context / "extra.txt").write_text("x")
    added = sim_image_tag(context)
    (context / "extra.txt").rename(context / "other.txt")

    assert len({tag, added, sim_image_tag(context)}) == 3


def test_cv_sim_tag_ignores_pycache(tmp_path: Path) -> None:
    context = _context(tmp_path)
    tag = sim_image_tag(context)

    for directory in (context, context / "joe"):
        cache = directory / "__pycache__"
        cache.mkdir(exist_ok=True)
        (cache / "sim.launch.cpython-312.pyc").write_bytes(b"compiled")

    assert sim_image_tag(context) == tag
