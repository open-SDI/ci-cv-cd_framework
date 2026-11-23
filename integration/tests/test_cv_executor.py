"""The CV run loop against the host's dockerd, with a fake ROS."""

from __future__ import annotations

import contextlib
import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import docker
import pytest
import requests

from sdi_pipeline_integration import _cv_run as run
from sdi_pipeline_integration._cv_adapter import warm_engine
from sdi_pipeline_integration._cv_result import REQUIREMENTS
from sdi_pipeline_integration._cv_run import (
    Blocked,
    ContainerState,
    CountingOutput,
    Errored,
    FailedToStart,
    Finished,
    Placement,
    Recording,
    RecordingFailed,
    RunRequest,
    run_composition,
)

from ._cv_images import amd64_digest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from docker import DockerClient

pytestmark = pytest.mark.slow

BUSYBOX = (
    "busybox:1.37@sha256:"
    "bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
)
HOST_DOCKER = os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
INSTALLATION = Path(__file__).parents[2] / "deployment" / "installation.env"
COUNTING = (
    CountingOutput(topic="/fake/out", type="std_msgs/msg/String", service_id="node"),
)
SIM_UP = "touch /dev/shm/cv-sim && exec sleep 3600"
NODE_UP = 'echo "fake node $*" && exec sleep 3600'
WRITE_RESULT = (
    "test -e /dev/shm/cv-sim || exit 9\n"
    'echo "counting $CV_COUNTING"\n'
    "sleep {delay}\n"
    "mkdir -p /cv\n"
    "cp /canned/result.json /cv/result.json\n"
)


def _passing_result() -> dict[str, object]:
    waits = {
        "accept-spoken-request": "/speech/text",
        "navigate-to-rendezvous": "/navigate_to_pose",
        "verify-recipient-face": "/face_recognition/name",
        "acknowledge-receipt": "/speech/text",
        "return-to-origin": "/navigate_to_pose",
    }
    steps: list[dict[str, object]] = [
        {
            "requirement_id": item,
            "status": "passed",
            "waited_on": waits[item],
            "elapsed_s": 1.0,
        }
        if item in waits
        else {"requirement_id": item, "status": "scripted"}
        for item in REQUIREMENTS
    ]
    return {
        "unloadable_types": [],
        "counts": {"/fake/out": 7},
        "mission": {"setup": {"status": "passed", "elapsed_s": 1.0}, "steps": steps},
        "graph": [
            {
                "topic": "/fake/out",
                "types": ["std_msgs/msg/String"],
                "publishers": ["/node"],
            }
        ],
    }


@dataclass(frozen=True)
class Host:
    """The host daemon, the test registry's address, and this module's tag."""

    client: DockerClient
    registry: str
    tag: str


@pytest.fixture(scope="module")
def host() -> Iterator[Host]:
    client = docker.DockerClient(base_url=HOST_DOCKER)
    tag = f"test-{uuid.uuid4().hex[:12]}"
    registry_image = next(
        line.split("=", 1)[1]
        for line in INSTALLATION.read_text().splitlines()
        if line.startswith("SDI_REGISTRY_IMAGE=")
    )
    registry = client.containers.run(
        registry_image,
        detach=True,
        labels={"org.sdi.cv-test-registry": tag},
        ports={"5000/tcp": ("127.0.0.1", 0)},
    )
    registry.reload()
    ports = cast("dict[str, list[dict[str, str]]]", registry.ports)
    address = f"127.0.0.1:{ports['5000/tcp'][0]['HostPort']}"
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                requests.get(f"http://{address}/v2/", timeout=2).raise_for_status()
                break
            except requests.RequestException:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)
        yield Host(client, address, tag)
    finally:
        registry.remove(force=True)
        for image in client.images.list():
            names = [
                *image.tags,
                *cast("list[str]", image.attrs.get("RepoDigests") or []),
            ]
            if any(tag in name or address in name for name in names):
                client.images.remove(image.id, force=True)  # pyright: ignore[reportUnknownMemberType]
        client.close()


@pytest.fixture
def engine(host: Host, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Host:
    monkeypatch.setattr(run, "REGISTRY", host.registry)
    monkeypatch.setattr(run, "EXECUTION_LABEL", f"org.sdi.cv-{host.tag}")
    monkeypatch.setattr(run, "SIM_REPOSITORY", f"sdi-cv-sim-{host.tag}")
    _sim(tmp_path, monkeypatch, WRITE_RESULT.format(delay=1))
    return host


def _sim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orchestrator: str,
    *,
    sim: str = SIM_UP,
    result: bytes | None = None,
) -> None:
    context = tmp_path / "sim"
    (context / "canned").mkdir(parents=True, exist_ok=True)
    (context / "canned" / "result.json").write_bytes(
        result if result is not None else json.dumps(_passing_result()).encode()
    )
    script = context / "cv-orchestrator"
    script.write_text(f"#!/bin/sh\n{orchestrator}")
    script.chmod(0o755)
    (context / "Dockerfile").write_text(
        f"FROM {BUSYBOX}\n"
        "COPY cv-orchestrator /usr/local/bin/cv-orchestrator\n"
        "COPY canned/ /canned/\n"
        f"CMD {json.dumps(['sh', '-c', sim])}\n"
    )
    monkeypatch.setattr(run, "SIM_CONTEXT", context)


def _placement(
    host: Host,
    tmp_path: Path,
    service_id: str,
    *,
    ros2: str | None = NODE_UP,
    command: list[str] | None = None,
) -> Placement:
    context = tmp_path / f"placement-{service_id}"
    context.mkdir()
    dockerfile = f"FROM {BUSYBOX}\n"
    if ros2 is not None:
        script = context / "ros2"
        script.write_text(f"#!/bin/sh\n{ros2}\n")
        script.chmod(0o755)
        dockerfile += "COPY ros2 /usr/local/bin/ros2\n"
    dockerfile += f"CMD {json.dumps(command or ['ros2', 'run', 'fake', 'node'])}\n"
    (context / "Dockerfile").write_text(dockerfile)
    reference = f"{host.registry}/sdi/{service_id}:{host.tag}"
    host.client.images.build(path=str(context), tag=reference, rm=True)
    pushed = host.client.images.push(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        f"{host.registry}/sdi/{service_id}", tag=host.tag, stream=True, decode=True
    )
    for line in cast("Iterator[dict[str, object]]", pushed):
        assert "error" not in line, line
    digest = amd64_digest(f"http://{host.registry}", f"sdi/{service_id}", host.tag)
    host.client.images.remove(reference)  # pyright: ignore[reportUnknownMemberType]
    return Placement(service_id, digest)


def _request(
    *placements: Placement, wait_s: float = 60, recording_max_bytes: int | None = None
) -> RunRequest:
    return RunRequest(
        execution_id=str(uuid.uuid4()),
        robot="waffle_pi",
        placements=placements,
        counting=COUNTING,
        deadline_monotonic=time.monotonic() + run.TEARDOWN_RESERVE_SECONDS + wait_s,
        recording_max_bytes=recording_max_bytes,
    )


def _remaining(host: Host) -> list[str]:
    return [
        str(item.name)
        for item in host.client.containers.list(
            all=True, filters={"label": run.EXECUTION_LABEL}
        )
    ]


def _started(state: ContainerState | FailedToStart) -> ContainerState:
    assert isinstance(state, ContainerState)
    return state


def test_cv_executor_runs_a_composition_and_reads_its_result(
    engine: Host, tmp_path: Path
) -> None:
    node = _placement(engine, tmp_path, "fake-node")

    outcome = run_composition(_request(node), HOST_DOCKER)

    assert isinstance(outcome, Finished)
    assert outcome.result.counts == {"/fake/out": 7}
    assert outcome.result.mission is not None
    assert outcome.result.mission.completed
    assert outcome.snapshot.sim.running
    assert outcome.snapshot.orchestrator.status == "exited"
    assert outcome.snapshot.orchestrator.exit_code == 0
    counting = json.dumps(
        [item.model_dump() for item in COUNTING], separators=(",", ":")
    )
    assert f"counting {counting}" in outcome.snapshot.orchestrator.log_tail
    placement = _started(outcome.snapshot.placements["fake-node"])
    assert placement.running
    assert placement.log_tail == (
        "fake node run fake node --ros-args -p use_sim_time:=true",
    )
    assert set(outcome.phases) == {
        "leftover-cleanup",
        "sim-build",
        "pulls",
        "start",
        "orchestrator-wait",
    }
    assert _remaining(engine) == []


def test_cv_executor_removes_leftovers_of_its_label_only(
    engine: Host, tmp_path: Path
) -> None:
    node = _placement(engine, tmp_path, "fake-node")
    leftover = engine.client.containers.run(
        BUSYBOX, ["sleep", "3600"], detach=True, labels={run.EXECUTION_LABEL: "earlier"}
    )
    bystander = engine.client.containers.run(
        BUSYBOX,
        ["sleep", "3600"],
        detach=True,
        labels={f"{run.EXECUTION_LABEL}-bystander": "earlier"},
    )
    try:
        outcome = run_composition(_request(node), HOST_DOCKER)

        assert isinstance(outcome, Finished)
        survivors = {item.id for item in engine.client.containers.list(all=True)}
        assert leftover.id not in survivors
        bystander.reload()
        assert bystander.status == "running"
    finally:
        bystander.remove(force=True)
        if leftover.id in {item.id for item in engine.client.containers.list(all=True)}:
            leftover.remove(force=True)


def test_cv_executor_bounds_the_orchestrator_wait_by_the_work_limit(
    engine: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sim(tmp_path, monkeypatch, "exec sleep 3600")
    node = _placement(engine, tmp_path, "fake-node")
    request = _request(node, wait_s=3)

    outcome = run_composition(request, HOST_DOCKER)

    assert isinstance(outcome, Errored)
    assert outcome.error == "orchestrator-timeout"
    assert outcome.snapshot is not None
    assert outcome.snapshot.orchestrator.running
    assert (
        time.monotonic()
        < request.deadline_monotonic - run.TEARDOWN_RESERVE_SECONDS + 30
    )
    assert _remaining(engine) == []


def test_cv_executor_snapshots_an_exited_placement_before_teardown(
    engine: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sim(tmp_path, monkeypatch, WRITE_RESULT.format(delay=4))
    crashed = _placement(
        engine, tmp_path, "crashing-node", ros2="echo 'fake node failed'; exit 3"
    )
    healthy = _placement(engine, tmp_path, "fake-node")
    unstartable = _placement(engine, tmp_path, "no-ros2", ros2=None)

    outcome = run_composition(_request(crashed, healthy, unstartable), HOST_DOCKER)

    assert isinstance(outcome, Finished)
    exited = _started(outcome.snapshot.placements["crashing-node"])
    assert (exited.status, exited.exit_code) == ("exited", 3)
    assert exited.log_tail == ("fake node failed",)
    assert _started(outcome.snapshot.placements["fake-node"]).running
    failed = outcome.snapshot.placements["no-ros2"]
    assert isinstance(failed, FailedToStart)
    assert "ros2" in failed.detail
    assert _remaining(engine) == []


def test_cv_executor_refuses_a_result_over_64_kib_by_its_size(
    engine: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oversized = _passing_result()
    oversized["graph"] = [
        {"topic": f"/t{index}", "types": ["std_msgs/msg/String"], "publishers": ["/n"]}
        for index in range(1000)
    ]
    content = json.dumps(oversized).encode()
    assert len(content) > run.RESULT_MAX_BYTES
    _sim(tmp_path, monkeypatch, WRITE_RESULT.format(delay=0), result=content)

    outcome = run_composition(
        _request(_placement(engine, tmp_path, "fake-node")), HOST_DOCKER
    )

    assert isinstance(outcome, Errored)
    assert outcome.error == "result-too-large"
    assert _remaining(engine) == []


def test_cv_executor_reports_an_orchestrator_that_wrote_no_result(
    engine: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sim(tmp_path, monkeypatch, "exit 0")

    outcome = run_composition(
        _request(_placement(engine, tmp_path, "fake-node")), HOST_DOCKER
    )

    assert isinstance(outcome, Errored)
    assert outcome.error == "no-result"
    assert _remaining(engine) == []


def test_cv_executor_reports_an_invalid_result(
    engine: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sim(tmp_path, monkeypatch, WRITE_RESULT.format(delay=0), result=b'{"counts": {}}')

    outcome = run_composition(
        _request(_placement(engine, tmp_path, "fake-node")), HOST_DOCKER
    )

    assert isinstance(outcome, Errored)
    assert outcome.error == "invalid-result"


# The fake sim "records" its CV_RECORDING value, so the bytes read back show
# which value the executor passed.
RECORDING_SIM = (
    'mkdir -p /cv/recording && printf %s "$CV_RECORDING"'
    " > /cv/recording/sim-recording.mp4 && " + SIM_UP
)
RECORDER_SUMMARY = "subscribed True frames 3 skipped 0 ffmpeg exit 0"


def test_cv_executor_reads_the_recording_after_the_result(
    engine: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sim(tmp_path, monkeypatch, WRITE_RESULT.format(delay=1), sim=RECORDING_SIM)
    monkeypatch.setattr(run, "RECORDER_STOP", ("echo", RECORDER_SUMMARY))
    node = _placement(engine, tmp_path, "fake-node")

    outcome = run_composition(_request(node, recording_max_bytes=1024), HOST_DOCKER)

    assert isinstance(outcome, Finished)
    assert outcome.result.counts == {"/fake/out": 7}
    assert isinstance(outcome.recording, Recording)
    assert outcome.recording.video == b"1"
    assert outcome.recording.summary == RECORDER_SUMMARY
    assert _remaining(engine) == []


def test_cv_executor_records_nothing_unless_the_request_keeps_a_recording(
    engine: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sim(tmp_path, monkeypatch, WRITE_RESULT.format(delay=1), sim=RECORDING_SIM)
    monkeypatch.setattr(run, "RECORDER_STOP", ("echo", RECORDER_SUMMARY))

    outcome = run_composition(
        _request(_placement(engine, tmp_path, "fake-node")), HOST_DOCKER
    )

    assert isinstance(outcome, Finished)
    assert outcome.recording is None


def _failed_recording(
    engine: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, max_bytes: int
) -> RecordingFailed:
    _sim(tmp_path, monkeypatch, WRITE_RESULT.format(delay=1), sim=RECORDING_SIM)
    node = _placement(engine, tmp_path, "fake-node")

    outcome = run_composition(
        _request(node, recording_max_bytes=max_bytes), HOST_DOCKER
    )

    assert isinstance(outcome, Finished)
    assert outcome.result.mission is not None
    assert outcome.result.mission.completed
    assert _remaining(engine) == []
    assert isinstance(outcome.recording, RecordingFailed)
    return outcome.recording


def test_cv_executor_keeps_the_result_when_the_recorder_fails(
    engine: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # busybox has no python3, so the real stop command exits 127.
    failed = _failed_recording(engine, tmp_path, monkeypatch, 1024)

    assert failed.detail.startswith("the recorder stop exited 127")


def test_cv_executor_keeps_the_result_when_the_recording_is_over_its_limit(
    engine: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run, "RECORDER_STOP", ("echo", RECORDER_SUMMARY))

    failed = _failed_recording(engine, tmp_path, monkeypatch, 0)

    assert failed.detail == "the recording is 1 bytes, over 0"


def test_cv_executor_reports_a_sim_that_stopped(
    engine: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sim(
        tmp_path,
        monkeypatch,
        "sleep 4\nmkdir -p /cv\ncp /canned/result.json /cv/result.json\n",
        sim="sleep 1",
    )

    outcome = run_composition(
        _request(_placement(engine, tmp_path, "fake-node")), HOST_DOCKER
    )

    assert isinstance(outcome, Errored)
    assert outcome.error == "sim-not-running"
    assert outcome.snapshot is not None
    assert outcome.snapshot.sim.status == "exited"
    assert _remaining(engine) == []


def test_cv_executor_starts_nothing_when_a_placement_is_blocked(
    engine: Host, tmp_path: Path
) -> None:
    unsupported = _placement(
        engine, tmp_path, "shell-node", command=["sh", "-c", "sleep 3600"]
    )
    missing = Placement("missing-node", "sha256:" + "0" * 64)
    healthy = _placement(engine, tmp_path, "fake-node")

    outcome = run_composition(_request(unsupported, missing, healthy), HOST_DOCKER)

    assert isinstance(outcome, Blocked)
    assert outcome.causes == {
        "shell-node": "unsupported-command",
        "missing-node": "image-unavailable",
    }
    assert "start" not in outcome.phases
    assert _remaining(engine) == []


def _sim_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    context = tmp_path / "unbuildable-sim"
    context.mkdir()
    (context / "Dockerfile").write_text(f"FROM {BUSYBOX}\n{body}\n")
    monkeypatch.setattr(run, "SIM_CONTEXT", context)


def test_cv_executor_reports_a_sim_context_the_engine_refuses(
    engine: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sim_context(tmp_path, monkeypatch, "NOTANINSTRUCTION x")

    outcome = run_composition(
        _request(_placement(engine, tmp_path, "fake-node")), HOST_DOCKER
    )

    assert isinstance(outcome, Errored)
    assert outcome.error == "sim-build-failed"
    assert "unknown instruction" in outcome.detail
    assert set(outcome.phases) == {"leftover-cleanup", "sim-build"}
    assert _remaining(engine) == []


@pytest.mark.usefixtures("engine")
def test_cv_executor_reports_an_engine_silent_during_the_sim_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run, "ENGINE_SILENCE_SECONDS", 2)
    _sim_context(tmp_path, monkeypatch, "RUN sleep 6")

    outcome = run_composition(
        _request(Placement("fake-node", "sha256:" + "0" * 64)), HOST_DOCKER
    )

    assert isinstance(outcome, Errored)
    assert outcome.error == "sim-build-failed"
    assert set(outcome.phases) == {"leftover-cleanup", "sim-build"}
    assert outcome.phases["sim-build"] >= 2


def test_cv_executor_warm_builds_the_sim_image_the_stage_then_reuses(
    engine: Host,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unique = f"# {uuid.uuid4().hex}\n"
    _sim(tmp_path, monkeypatch, WRITE_RESULT.format(delay=1) + unique)
    tag = run.sim_image_tag(run.SIM_CONTEXT)

    assert warm_engine(HOST_DOCKER) == 0
    built = capsys.readouterr().out.splitlines()
    assert warm_engine(HOST_DOCKER) == 0
    present = capsys.readouterr().out.splitlines()
    outcome = run_composition(
        _request(_placement(engine, tmp_path, "fake-node")), HOST_DOCKER
    )

    assert [line.split(" : ")[0] for line in built if line.startswith("Step ")] == [
        "Step 1/4",
        "Step 2/4",
        "Step 3/4",
        "Step 4/4",
    ]
    assert built[-1].startswith(f"sdi-cv-warm: {tag} built in ")
    assert present[-1].startswith(f"sdi-cv-warm: {tag} present in ")
    assert not [line for line in present if line.startswith("Step ")]
    assert isinstance(outcome, Finished)


@pytest.mark.usefixtures("engine")
def test_cv_executor_warm_reports_a_sim_image_that_does_not_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _sim_context(tmp_path, monkeypatch, "RUN exit 3")

    assert warm_engine(HOST_DOCKER) == 1

    captured = capsys.readouterr()
    steps = [line for line in captured.out.splitlines() if line.startswith("Step ")]
    assert [line.split(" : ")[0] for line in steps] == ["Step 1/2", "Step 2/2"]
    assert captured.err.startswith("sdi-cv-warm: sim-build-failed: "), captured.err
    assert "returned a non-zero code: 3" in captured.err
    assert " built in " not in captured.out


class _EngineProxy:
    def __init__(
        self, drop_on: bytes = b"", *, after: float = 1, withhold: bool = False
    ) -> None:
        self.drop_on = drop_on
        self.after = after
        self.withhold = withhold
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.url = f"tcp://127.0.0.1:{self.listener.getsockname()[1]}"
        self.connections: list[socket.socket] = []
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self) -> None:
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            daemon = socket.socket(socket.AF_UNIX)
            daemon.connect(HOST_DOCKER.removeprefix("unix://"))
            self.connections += [client, daemon]
            for source, target in ((client, daemon), (daemon, client)):
                threading.Thread(
                    target=self.pipe, args=(source, target), daemon=True
                ).start()

    def pipe(self, source: socket.socket, target: socket.socket) -> None:
        try:
            while data := source.recv(65536):
                if self.drop_on and self.drop_on in data:
                    threading.Timer(self.after, self.drop, (source, target)).start()
                    if self.withhold:
                        return
                target.sendall(data)
        except OSError:
            return

    def drop(self, *connections: socket.socket) -> None:
        for connection in connections:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            connection.close()

    def stop(self) -> None:
        self.drop(self.listener, *self.connections)


def test_cv_executor_reports_a_pull_the_engine_dropped(
    engine: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    silent = engine.client.containers.run(
        BUSYBOX,
        ["nc", "-lk", "-p", "5000", "-e", "sleep", "3600"],
        detach=True,
        labels={"org.sdi.cv-test-registry": engine.tag},
        ports={"5000/tcp": ("127.0.0.1", 0)},
    )
    proxy = _EngineProxy(drop_on=b"/images/create?")
    try:
        silent.reload()
        ports = cast("dict[str, list[dict[str, str]]]", silent.ports)
        monkeypatch.setattr(
            run, "REGISTRY", f"127.0.0.1:{ports['5000/tcp'][0]['HostPort']}"
        )

        outcome = run_composition(
            _request(Placement("fake-node", "sha256:" + "0" * 64)), proxy.url
        )
    finally:
        proxy.stop()
        silent.remove(force=True)

    assert isinstance(outcome, Errored)
    assert outcome.error == "pull-failed"
    assert "fake-node" in outcome.detail
    assert set(outcome.phases) == {"leftover-cleanup", "sim-build", "pulls"}
    assert outcome.phases["pulls"] < 30
    assert _remaining(engine) == []


def test_cv_executor_reports_a_pull_the_engine_left_unanswered(
    engine: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run, "ENGINE_SILENCE_SECONDS", 2)
    proxy = _EngineProxy(drop_on=b"/images/create?", after=20, withhold=True)
    try:
        outcome = run_composition(
            _request(Placement("fake-node", "sha256:" + "0" * 64)), proxy.url
        )
    finally:
        proxy.stop()

    assert isinstance(outcome, Errored)
    assert outcome.error == "pull-failed"
    assert "fake-node" in outcome.detail
    assert set(outcome.phases) == {"leftover-cleanup", "sim-build", "pulls"}
    assert 2 <= outcome.phases["pulls"] < 10
    assert _remaining(engine) == []


def test_cv_executor_reports_a_placement_start_the_engine_dropped(
    engine: Host, tmp_path: Path
) -> None:
    request = _request(_placement(engine, tmp_path, "fake-node"))
    proxy = _EngineProxy(
        drop_on=f"name=cv-{request.execution_id}-fake-node".encode(),
        after=0,
        withhold=True,
    )
    try:
        outcome = run_composition(request, proxy.url)
    finally:
        proxy.stop()

    assert isinstance(outcome, Errored)
    assert outcome.error == "start-failed"
    assert "fake-node" in outcome.detail
    assert "start" in outcome.phases
    assert _remaining(engine) == []


def test_cv_executor_reports_a_sim_that_did_not_start(
    engine: Host, tmp_path: Path
) -> None:
    request = _request(_placement(engine, tmp_path, "fake-node"))
    squatter = engine.client.containers.run(
        BUSYBOX,
        ["sleep", "3600"],
        detach=True,
        name=f"cv-{request.execution_id}-sim",
        labels={"org.sdi.cv-test-registry": engine.tag},
    )
    try:
        outcome = run_composition(request, HOST_DOCKER)
    finally:
        squatter.remove(force=True)

    assert isinstance(outcome, Errored)
    assert outcome.error == "start-failed"
    assert outcome.detail.startswith("the sim did not start")
    assert "start" in outcome.phases
    assert _remaining(engine) == []


def test_cv_executor_reports_an_orchestrator_that_did_not_start(
    engine: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run, "ORCHESTRATOR_COMMAND", ("no-such-orchestrator",))

    outcome = run_composition(
        _request(_placement(engine, tmp_path, "fake-node")), HOST_DOCKER
    )

    assert isinstance(outcome, Errored)
    assert outcome.error == "start-failed"
    assert outcome.detail.startswith("the orchestrator did not start")
    assert "start" in outcome.phases
    assert _remaining(engine) == []


def test_cv_executor_starts_nothing_once_the_work_limit_ran_out(
    engine: Host, tmp_path: Path
) -> None:
    outcome = run_composition(
        _request(_placement(engine, tmp_path, "fake-node"), wait_s=0), HOST_DOCKER
    )

    assert isinstance(outcome, Errored)
    assert outcome.error == "no-time-to-start"
    assert set(outcome.phases) == {"leftover-cleanup", "sim-build", "pulls"}
    assert _remaining(engine) == []


def test_cv_executor_keeps_the_first_error_when_teardown_fails(
    engine: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = _placement(engine, tmp_path, "fake-node")
    proxy = _EngineProxy()
    closed: list[bool] = []
    close = docker.DockerClient.close

    def snapshot_as_the_engine_goes_away(_: object) -> ContainerState:
        proxy.stop()
        message = "snapshot failed"
        raise RuntimeError(message)

    def recording_close(client: DockerClient) -> None:
        closed.append(True)
        close(client)

    monkeypatch.setattr(run, "_state", snapshot_as_the_engine_goes_away)
    monkeypatch.setattr(docker.DockerClient, "close", recording_close)
    try:
        with pytest.raises(RuntimeError, match="snapshot failed") as raised:
            run_composition(_request(node), proxy.url)
    finally:
        for container in engine.client.containers.list(
            all=True, filters={"label": run.EXECUTION_LABEL}
        ):
            container.remove(force=True)

    assert any("teardown" in note for note in raised.value.__notes__)
    assert closed == [True]


def test_cv_executor_tears_down_after_an_exception_mid_run(
    engine: Host, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = _placement(engine, tmp_path, "fake-node")
    seen: list[str] = []

    def broken_snapshot(_: object) -> ContainerState:
        seen.extend(_remaining(engine))
        message = "snapshot failed"
        raise RuntimeError(message)

    monkeypatch.setattr(run, "_state", broken_snapshot)

    with pytest.raises(RuntimeError, match="snapshot failed"):
        run_composition(_request(node), HOST_DOCKER)

    assert len(seen) == 3
    assert _remaining(engine) == []
