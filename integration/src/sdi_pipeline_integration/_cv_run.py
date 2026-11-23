from __future__ import annotations

import hashlib
import io
import json
import tarfile
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast

import requests
import urllib3
from pydantic import ValidationError

from ._contracts import ContractModel, NonBlank
from ._cv_result import ScenarioResult

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Mapping, Sequence

    from docker import DockerClient
    from docker.models.containers import Container
    from docker.models.images import Image

REGISTRY = "registry:5000"
EXECUTION_LABEL = "org.sdi.cv-execution"
SIM_CONTEXT = Path(__file__).parent / "_cv_sim" / "s-04"
SIM_REPOSITORY = "sdi-cv-sim"
SIM_SHM_SIZE = "1g"
ORCHESTRATOR_COMMAND = ("cv-orchestrator",)
RESULT_PATH = "/cv/result.json"
RESULT_MAX_BYTES = 64 * 1024
TEARDOWN_RESERVE_SECONDS = 120
ENGINE_SILENCE_SECONDS = 600
LOG_TAIL_LINES = 40
ENGINE_LOST = (requests.RequestException, urllib3.exceptions.HTTPError)

Robot = Literal["waffle_pi", "burger_cam"]
BlockingCause = Literal["image-unavailable", "unsupported-command"]
RunError = Literal[
    "sim-build-failed",
    "pull-failed",
    "no-time-to-start",
    "start-failed",
    "orchestrator-timeout",
    "sim-not-running",
    "no-result",
    "result-too-large",
    "invalid-result",
]
Phase = Literal["leftover-cleanup", "sim-build", "pulls", "start", "orchestrator-wait"]


class RunOptions(TypedDict, total=False):
    image: str
    command: list[str]
    name: str
    labels: dict[str, str]
    environment: dict[str, str]
    network_mode: str
    ipc_mode: str
    shm_size: str
    detach: Literal[True]


class CountingOutput(ContractModel):
    topic: NonBlank
    type: NonBlank
    service_id: NonBlank


@dataclass(frozen=True)
class Placement:
    service_id: str
    digest: str

    @property
    def reference(self) -> str:
        return f"{REGISTRY}/sdi/{self.service_id}@{self.digest}"


@dataclass(frozen=True)
class RunRequest:
    execution_id: str
    robot: Robot
    placements: tuple[Placement, ...]
    counting: tuple[CountingOutput, ...]
    deadline_monotonic: float


@dataclass(frozen=True)
class Plan:
    sim: RunOptions
    placements: Mapping[str, RunOptions]
    orchestrator: RunOptions


@dataclass(frozen=True)
class ContainerState:
    status: str
    exit_code: int
    log_tail: tuple[str, ...]

    @property
    def running(self) -> bool:
        return self.status == "running"


@dataclass(frozen=True)
class FailedToStart:
    detail: str


@dataclass(frozen=True)
class Snapshot:
    sim: ContainerState
    orchestrator: ContainerState
    placements: Mapping[str, ContainerState | FailedToStart]


@dataclass(frozen=True)
class Blocked:
    """A placement could not be pulled or run in sim time; nothing started."""

    causes: Mapping[str, BlockingCause]
    phases: Mapping[Phase, float]


@dataclass(frozen=True)
class Finished:
    result: ScenarioResult
    snapshot: Snapshot
    phases: Mapping[Phase, float]


@dataclass(frozen=True)
class Errored:
    error: RunError
    detail: str
    snapshot: Snapshot | None
    phases: Mapping[Phase, float]


RunOutcome = Blocked | Finished | Errored


@dataclass(frozen=True)
class SimImage:
    tag: str
    built: bool


@dataclass(frozen=True)
class SimBuildFailed:
    detail: str


type Progress = Callable[[str], None]


def sim_time_command(command: Sequence[str]) -> tuple[str, ...] | None:
    match list(command):
        case ["ros2", "launch", _, *_]:
            return (*command, "use_sim_time:=true")
        case ["ros2", "run", _, _, *_]:
            return (*command, "--ros-args", "-p", "use_sim_time:=true")
        case _:
            return None


def sim_image_tag(context: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in context.rglob("*")
        if path.is_file() and "__pycache__" not in path.relative_to(context).parts
    )
    for path in files:
        content = path.read_bytes()
        for part in (
            path.relative_to(context).as_posix().encode(),
            str(len(content)).encode(),
            content,
        ):
            digest.update(part + b"\0")
    return f"{SIM_REPOSITORY}:{digest.hexdigest()}"


def plan(
    request: RunRequest, sim_image: str, commands: Mapping[str, tuple[str, ...]]
) -> Plan:
    labels = {EXECUTION_LABEL: request.execution_id}
    sim = f"cv-{request.execution_id}-sim"
    counting = json.dumps(
        [item.model_dump(mode="json") for item in request.counting],
        separators=(",", ":"),
    )
    return Plan(
        sim={
            "image": sim_image,
            "name": sim,
            "labels": labels,
            "environment": {"CV_ROBOT": request.robot},
            "ipc_mode": "shareable",
            "shm_size": SIM_SHM_SIZE,
            "detach": True,
        },
        placements={
            item.service_id: {
                "image": item.reference,
                "command": list(commands[item.service_id]),
                "name": f"cv-{request.execution_id}-{item.service_id}",
                "labels": labels,
                "network_mode": f"container:{sim}",
                "ipc_mode": f"container:{sim}",
                "detach": True,
            }
            for item in request.placements
        },
        orchestrator={
            "image": sim_image,
            "command": list(ORCHESTRATOR_COMMAND),
            "name": f"cv-{request.execution_id}-orchestrator",
            "labels": labels,
            "environment": {"CV_COUNTING": counting},
            "network_mode": f"container:{sim}",
            "ipc_mode": f"container:{sim}",
            "detach": True,
        },
    )


def run_composition(request: RunRequest, engine_url: str) -> RunOutcome:
    import docker  # noqa: PLC0415
    from docker.errors import DockerException  # noqa: PLC0415

    label = f"{EXECUTION_LABEL}={request.execution_id}"
    with closing(
        docker.DockerClient(base_url=engine_url, timeout=ENGINE_SILENCE_SECONDS)
    ) as client:
        try:
            outcome = _Run(client, request).outcome()
        except BaseException as error:
            try:
                _remove(client, label)
            except (DockerException, *ENGINE_LOST) as teardown:
                error.add_note(f"teardown also failed: {teardown!r}")
            raise
        _remove(client, label)
        return outcome


def prepare_engine(engine_url: str, progress: Progress) -> SimImage | SimBuildFailed:
    import docker  # noqa: PLC0415

    with closing(
        docker.DockerClient(base_url=engine_url, timeout=ENGINE_SILENCE_SECONDS)
    ) as client:
        return _ensure_sim_image(client, progress)


class _Run:
    def __init__(self, client: DockerClient, request: RunRequest) -> None:
        self.client = client
        self.request = request
        self.phases: dict[Phase, float] = {}

    def outcome(self) -> RunOutcome:
        try:
            return self.run()
        except _StopRunError as stopped:
            return self.failed(stopped.error, stopped.detail, None)

    def run(self) -> RunOutcome:
        with self.timed("leftover-cleanup"):
            _remove(self.client, EXECUTION_LABEL)
        with self.timed("sim-build"):
            sim_image = self.sim_image()
        with self.timed("pulls"):
            commands, causes = self.pull()
        if causes:
            return Blocked(causes, self.phases)
        if self.remaining() <= 0:
            raise _StopRunError(
                error="no-time-to-start",
                detail="the work limit ran out during the sim build and pulls",
            )
        containers = plan(self.request, sim_image, commands)
        with self.timed("start"):
            sim = self.start_required("sim", containers.sim)
            placements = {
                service_id: self.start_placement(service_id, options)
                for service_id, options in containers.placements.items()
            }
            orchestrator = self.start_required("orchestrator", containers.orchestrator)
        with self.timed("orchestrator-wait"):
            exited = self.wait(orchestrator)
        snapshot = Snapshot(
            _state(sim),
            _state(orchestrator),
            {
                service_id: item if isinstance(item, FailedToStart) else _state(item)
                for service_id, item in placements.items()
            },
        )
        if not exited:
            return self.failed(
                "orchestrator-timeout",
                "the orchestrator was still running at the wait timeout",
                snapshot,
            )
        if not snapshot.sim.running:
            return self.failed(
                "sim-not-running",
                f"the sim was {snapshot.sim.status} when the orchestrator exited",
                snapshot,
            )
        return self.read_result(orchestrator, snapshot)

    def failed(
        self, error: RunError, detail: str, snapshot: Snapshot | None
    ) -> Errored:
        return Errored(error, detail, snapshot, self.phases)

    @contextmanager
    def timed(self, phase: Phase) -> Generator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            self.phases[phase] = round(time.monotonic() - started, 1)

    def sim_image(self) -> str:
        match _ensure_sim_image(self.client, _silent):
            case SimImage(tag=tag):
                return tag
            case SimBuildFailed(detail=detail):
                raise _StopRunError(error="sim-build-failed", detail=detail)

    def pull(self) -> tuple[dict[str, tuple[str, ...]], dict[str, BlockingCause]]:
        from docker.errors import DockerException  # noqa: PLC0415

        commands: dict[str, tuple[str, ...]] = {}
        causes: dict[str, BlockingCause] = {}
        for item in self.request.placements:
            try:
                image = self.pull_image(item.reference)
            except DockerException:
                causes[item.service_id] = "image-unavailable"
                continue
            except ENGINE_LOST as error:
                raise _StopRunError(
                    error="pull-failed",
                    detail=f"pulling {item.service_id} failed: {error}",
                ) from error
            config = cast("dict[str, object]", image.attrs.get("Config") or {})
            command = sim_time_command(cast("list[str]", config.get("Cmd") or []))
            if command is None:
                causes[item.service_id] = "unsupported-command"
            else:
                commands[item.service_id] = command
        return commands, causes

    def pull_image(self, reference: str) -> Image:
        # docker-py pulls with no read timeout, so a silent engine would hang it.
        from docker.errors import create_api_error_from_http_exception  # noqa: PLC0415

        api = self.client.api
        repository, digest = reference.split("@")
        with api.post(
            f"{api.base_url}/v{api.api_version}/images/create",
            params={"fromImage": repository, "tag": digest},
            stream=True,
            timeout=ENGINE_SILENCE_SECONDS,
        ) as response:
            try:
                response.raise_for_status()
            except requests.HTTPError as error:
                raise create_api_error_from_http_exception(error) from error
            for _ in response.iter_content(chunk_size=None):
                pass
        return self.client.images.get(reference)

    def start_required(self, role: str, options: RunOptions) -> Container:
        from docker.errors import DockerException  # noqa: PLC0415

        try:
            return self.client.containers.run(**options)
        except (DockerException, *ENGINE_LOST) as error:
            raise _StopRunError(
                error="start-failed", detail=f"the {role} did not start: {error}"
            ) from error

    def start_placement(
        self, service_id: str, options: RunOptions
    ) -> Container | FailedToStart:
        from docker.errors import APIError  # noqa: PLC0415

        try:
            return self.client.containers.run(**options)
        except APIError as error:
            return FailedToStart(str(error.explanation or error))
        except ENGINE_LOST as error:
            raise _StopRunError(
                error="start-failed",
                detail=f"the placement {service_id} did not start: {error}",
            ) from error

    def remaining(self) -> float:
        return (
            self.request.deadline_monotonic
            - TEARDOWN_RESERVE_SECONDS
            - time.monotonic()
        )

    def wait(self, orchestrator: Container) -> bool:
        remaining = self.remaining()
        if remaining <= 0:
            return False
        try:
            orchestrator.wait(timeout=remaining)
        except (requests.ConnectionError, requests.ReadTimeout):
            return False
        return True

    def read_result(self, orchestrator: Container, snapshot: Snapshot) -> RunOutcome:
        from docker.errors import NotFound  # noqa: PLC0415

        try:
            stream, stat = orchestrator.get_archive(RESULT_PATH)
        except NotFound:
            return self.failed(
                "no-result", f"the orchestrator wrote no {RESULT_PATH}", snapshot
            )
        size = cast("int", cast("dict[str, object]", stat)["size"])
        if size > RESULT_MAX_BYTES:
            return self.failed(
                "result-too-large",
                f"{RESULT_PATH} is {size} bytes, over {RESULT_MAX_BYTES}",
                snapshot,
            )
        archive = b"".join(stream)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            member = tar.next()
            content = tar.extractfile(member) if member is not None else None
            raw = content.read(RESULT_MAX_BYTES + 1) if content is not None else b""
        try:
            result = ScenarioResult.model_validate_json(raw)
        except ValidationError as error:
            return self.failed(
                "invalid-result",
                f"{RESULT_PATH} is not a valid result: {error.error_count()} errors",
                snapshot,
            )
        expected = {item.topic for item in self.request.counting}
        if not result.unloadable_types and set(result.counts) != expected:
            return self.failed(
                "invalid-result",
                f"{RESULT_PATH} does not count exactly the counting topics",
                snapshot,
            )
        return Finished(result, snapshot, self.phases)


class _StopRunError(Exception):
    def __init__(self, error: RunError, detail: str) -> None:
        super().__init__(detail)
        self.error: RunError = error
        self.detail = detail


def _ensure_sim_image(
    client: DockerClient, progress: Progress
) -> SimImage | SimBuildFailed:
    from docker.errors import DockerException  # noqa: PLC0415

    tag = sim_image_tag(SIM_CONTEXT)
    try:
        if client.images.list(name=tag):
            return SimImage(tag, built=False)
        detail = "the build ended without an image"
        for chunk in client.api.build(
            path=str(SIM_CONTEXT),
            tag=tag,
            rm=True,
            forcerm=True,
            decode=True,
            timeout=ENGINE_SILENCE_SECONDS,
        ):
            entry = cast("dict[str, object]", chunk)
            if "stream" in entry:
                progress(str(entry["stream"]))
            detail = str(entry.get("error") or entry.get("message") or detail)
        if client.images.list(name=tag):
            return SimImage(tag, built=True)
    except (DockerException, *ENGINE_LOST) as error:
        detail = str(error)
    return SimBuildFailed(detail)


def _silent(_: str) -> None:
    return None


def _remove(client: DockerClient, label: str) -> None:
    for container in client.containers.list(all=True, filters={"label": label}):
        container.remove(force=True)


def _state(container: Container) -> ContainerState:
    container.reload()
    state = cast("dict[str, object]", container.attrs["State"])
    logs = container.logs(tail=LOG_TAIL_LINES)
    return ContainerState(
        str(state["Status"]),
        cast("int", state["ExitCode"]),
        tuple(logs.decode(errors="replace").splitlines()),
    )
