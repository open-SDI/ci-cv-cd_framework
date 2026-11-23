"""CD's decisions that need no cluster: gaps, rendering, and the pass rule."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, get_args

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter
from pydantic.alias_generators import to_camel
from ruamel.yaml import YAML

from ._domain_contracts import (
    BuiltImageRecord,
    DeployedPlacement,
    DeploymentResult,
    FailedPlacement,
    KubernetesImageId,
    PodPhase,
    UndeployedPlacement,
    deployment_outcome,
    image_digest,
)
from ._image_build_redaction import failure_text
from ._yaml_input import validate_contract

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ._contracts import TargetExecutionProfile
    from ._domain_contracts import (
        CompositionBlueprint,
        DeploymentGap,
        DeploymentSchema,
        ImageBuildResult,
        ImageRecord,
        PlacementRecord,
        ServicePlacement,
        SourceInputDigest,
    )

CLUSTER_HOSTS = ("burger", "waffle", "orin", "xycar")
SERVICE_LABEL = "sdi/service"
HOST_LABEL = "sdi/host"
YAML_DIRECTIVE = "%YAML 1.1\n---\n"

type Manifest = dict[str, JsonValue]


@dataclass(frozen=True)
class CdInputs:
    profile: TargetExecutionProfile
    blueprint: CompositionBlueprint
    deployment: DeploymentSchema
    image_build: ImageBuildResult
    source_inputs: tuple[SourceInputDigest, ...]


@dataclass(frozen=True)
class Pin:
    """A placement cleared for the cluster, with its per-platform image."""

    service_id: str
    host: str
    image: str

    @property
    def digest(self) -> str:
        return image_digest(self.image)


@dataclass(frozen=True)
class Gap:
    """The not-evaluated records of a run where a placement failed its checks."""

    placements: tuple[UndeployedPlacement, ...]


def pin(inputs: CdInputs) -> tuple[Pin, ...] | Gap:
    """Clear every placement for the cluster, or record why none is deployed."""
    images = {item.service_id: item for item in inputs.image_build.images}
    checked = [
        _check(item, images.get(item.service_id), inputs.profile)
        for item in inputs.deployment.placements
    ]
    pins = tuple(item for item in checked if isinstance(item, Pin))
    if len(pins) == len(checked):
        return pins
    return Gap(
        tuple(
            UndeployedPlacement.model_validate(
                {"service_id": placement.service_id, "status": "not_evaluated"}
                | ({} if placement.host is None else {"host": placement.host})
                | {"reason": "blocked" if isinstance(item, Pin) else item}
            )
            for placement, item in zip(
                inputs.deployment.placements, checked, strict=True
            )
        )
    )


def _check(
    placement: ServicePlacement,
    image: ImageRecord | None,
    profile: TargetExecutionProfile,
) -> Pin | DeploymentGap:
    if placement.status == "unresolved" or placement.host is None:
        return "unresolved-placement"
    if not isinstance(image, BuiltImageRecord):
        return "image-not-built"
    architecture = profile.hosts[placement.host].architecture
    digest = next(
        (item.digest for item in image.platforms if item.architecture == architecture),
        None,
    )
    if digest is None:
        return "no-image-for-architecture"
    if placement.host not in CLUSTER_HOSTS:
        return "host-not-in-cluster"
    return Pin(
        placement.service_id,
        placement.host,
        f"registry:5000/{image.repository}@{digest}",
    )


def render(pins: Sequence[Pin]) -> Manifest:
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": item.service_id},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {SERVICE_LABEL: item.service_id}},
                    "template": {
                        "metadata": {"labels": {SERVICE_LABEL: item.service_id}},
                        "spec": {
                            "hostNetwork": True,
                            "nodeSelector": {HOST_LABEL: item.host},
                            "containers": [
                                {"name": item.service_id, "image": item.image}
                            ],
                        },
                    },
                },
            }
            for item in pins
        ],
    }


def manifest_yaml(manifest: Mapping[str, JsonValue]) -> bytes:
    """Return the manifest as the YAML bytes both kubectl and people read."""
    yaml = YAML(typ="safe", pure=True)
    yaml.default_flow_style = False
    yaml.width = 4096
    # kubectl reads YAML 1.1, where a bare yes, on, or n is a boolean, so the
    # 1.1 resolver decides what to quote. kubectl splits documents on ---, so
    # the %YAML 1.1 directive and --- that ruamel then emits are stripped.
    yaml.version = (1, 1)
    yaml.representer.sort_base_mapping_type_on_output = False
    stream = io.StringIO()
    yaml.dump(dict(manifest), stream)  # pyright: ignore[reportUnknownMemberType]
    return stream.getvalue().removeprefix(YAML_DIRECTIVE).encode()


class _Kubernetes(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, frozen=True)


class _Waiting(_Kubernetes):
    reason: str = ""
    message: str = ""


class _Terminated(_Kubernetes):
    reason: str = ""
    message: str = ""
    exit_code: int


class _State(_Kubernetes):
    waiting: _Waiting | None = None
    running: dict[str, JsonValue] | None = None
    terminated: _Terminated | None = None


class _ContainerStatus(_Kubernetes):
    image_id: str = Field(default="", alias="imageID")
    restart_count: int
    state: _State = _State()
    last_state: _State = _State()


class _Condition(_Kubernetes):
    type: str
    status: str
    reason: str = ""
    message: str = ""


class _PodStatus(_Kubernetes):
    phase: str = ""
    conditions: list[_Condition] = []
    container_statuses: list[_ContainerStatus] = []


class _PodSpec(_Kubernetes):
    node_name: str | None = None


class _PodMetadata(_Kubernetes):
    labels: dict[str, str] = {}


class _Pod(_Kubernetes):
    metadata: _PodMetadata
    spec: _PodSpec
    status: _PodStatus = _PodStatus()


class _PodList(_Kubernetes):
    items: list[_Pod]


_IMAGE_ID: TypeAdapter[str] = TypeAdapter(KubernetesImageId)
_PHASES = frozenset(get_args(PodPhase))


def judge(
    pins: Sequence[Pin], pods_json: str | bytes
) -> tuple[DeployedPlacement | FailedPlacement, ...]:
    """Apply the pass rule to `kubectl get pods -o json` output, one record per pin."""
    pods = _PodList.model_validate_json(pods_json).items
    return tuple(
        _judge(
            item,
            [
                pod
                for pod in pods
                if pod.metadata.labels.get(SERVICE_LABEL) == item.service_id
            ],
        )
        for item in pins
    )


def _judge(pin: Pin, pods: list[_Pod]) -> DeployedPlacement | FailedPlacement:
    base = {"service_id": pin.service_id, "host": pin.host, "image": pin.image}
    if len(pods) != 1:
        detail = (
            "no pod was created" if not pods else f"{len(pods)} pods for one replica"
        )
        return FailedPlacement.model_validate(
            base | {"status": "failed", "detail": detail}
        )
    pod = pods[0]
    container = next(iter(pod.status.container_statuses), None)
    observed: dict[str, object] = {}
    if pod.spec.node_name is not None:
        observed["node"] = pod.spec.node_name
    if container is not None and _is_image_id(container.image_id):
        observed["image_id"] = container.image_id
    if pod.status.phase in _PHASES:
        observed["phase"] = pod.status.phase
    if container is not None:
        observed["restart_count"] = container.restart_count
    problems = _problems(pin, pod, container)
    if not problems:
        return DeployedPlacement.model_validate(
            base | observed | {"status": "deployed"}
        )
    detail = failure_text("; ".join(problems), fallback="the pod broke the pass rule")
    return FailedPlacement.model_validate(
        base | observed | {"status": "failed", "detail": detail}
    )


def _problems(pin: Pin, pod: _Pod, container: _ContainerStatus | None) -> list[str]:
    problems = _placement_problems(pin, pod)
    if container is None:
        if pod.spec.node_name is not None:
            problems.append(
                f"phase {pod.status.phase or 'unknown'}, no container status"
            )
        return problems
    return problems + _container_problems(pin, pod.status.phase, container)


def _placement_problems(pin: Pin, pod: _Pod) -> list[str]:
    if pod.spec.node_name is None:
        unscheduled = next(
            (
                item
                for item in pod.status.conditions
                if item.type == "PodScheduled" and item.status == "False"
            ),
            None,
        )
        if unscheduled is None:
            return ["Pending: no node"]
        return [_reason(unscheduled.reason, unscheduled.message)]
    if pod.spec.node_name != pin.host:
        return [f"on node {pod.spec.node_name}, not {pin.host}"]
    return []


def _container_problems(pin: Pin, phase: str, container: _ContainerStatus) -> list[str]:
    problems: list[str] = []
    state, last = container.state, container.last_state
    if state.waiting is not None:
        problems.append(_reason(state.waiting.reason, state.waiting.message))
    elif state.terminated is not None:
        problems.append(_terminated(state.terminated))
    if container.restart_count > 0:
        restarts = f"restarted {container.restart_count} times"
        if last.terminated is not None:
            restarts = f"{restarts}, last {_terminated(last.terminated)}"
        problems.append(restarts)
    if (
        _is_image_id(container.image_id)
        and image_digest(container.image_id) != pin.digest
    ):
        problems.append(
            f"ran {image_digest(container.image_id)}, not the pinned {pin.digest}"
        )
    if phase != "Running" and state.waiting is None:
        problems.append(f"phase {phase or 'unknown'}")
    if not problems and state.running is None:
        problems.append("the container is not running")
    if not problems and not _is_image_id(container.image_id):
        problems.append("the image ID is not a digest reference")
    return problems


def _reason(reason: str, message: str) -> str:
    return ": ".join(item for item in (reason, message) if item) or "no reason given"


def _terminated(state: _Terminated) -> str:
    text = f"{state.reason or 'Terminated'} (exit {state.exit_code})"
    return f"{text}: {state.message}" if state.message else text


def _is_image_id(value: str) -> bool:
    try:
        _IMAGE_ID.validate_python(value)
    except ValueError:
        return False
    return True


def deployment_result(
    inputs: CdInputs,
    placements: Sequence[PlacementRecord],
    manifest: Manifest | None = None,
) -> DeploymentResult:
    """Assemble the Deployment result; the contract derives and checks its outcome."""
    deployment = inputs.deployment
    records = [item.model_dump(mode="json", exclude_none=True) for item in placements]
    return validate_contract(
        DeploymentResult,
        {
            "schema_version": "sdi.deployment-result/v1",
            "evidence_basis": "implemented",
            "deployment_result_id": (
                f"{deployment.scenario_id}-{deployment.testcase_id}-"
                f"{deployment.combination_id}-deployment-result"
            ).lower(),
            "blueprint_id": inputs.blueprint.blueprint_id,
            "deployment_schema_id": deployment.deployment_schema_id,
            "image_build_result_id": inputs.image_build.image_build_result_id,
            "scenario_id": deployment.scenario_id,
            "testcase_id": deployment.testcase_id,
            "combination_id": deployment.combination_id,
            "profile_id": deployment.profile_id,
            "source_inputs": [
                item.model_dump(mode="json") for item in inputs.source_inputs
            ],
            "domain_outcome": deployment_outcome(placements),
            "cluster_kind": "stand_in",
            "placements": records,
        }
        | ({} if manifest is None else {"manifest": manifest}),
        "deployment-result.json",
    )
