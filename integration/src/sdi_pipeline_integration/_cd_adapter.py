from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel

from ._adapter_kit import SUCCEEDED, Outcome, Verdict, serve
from ._cd_deployment import (
    HOST_LABEL,
    SERVICE_LABEL,
    CdInputs,
    Gap,
    deployment_result,
    judge,
    manifest_yaml,
    pin,
    render,
)
from ._contracts import TargetExecutionProfile
from ._domain_contracts import (
    CompositionBlueprint,
    DeployedPlacement,
    DeploymentSchema,
    FailedPlacement,
    ImageBuildResult,
    SourceInputDigest,
    UndeployedPlacement,
)
from ._image_build_redaction import failure_text
from ._json_input import canonical_json, parse_json
from ._yaml_input import parse_yaml, validate_contract

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ._adapter_kit import Diagnostic, Invocation, Succeeded
    from ._cd_deployment import Manifest, Pin
    from ._domain_contracts import DeploymentResult, PlacementRecord

KUBECTL_TIMEOUT_SECONDS = 20
EVIDENCE_SECONDS = 20
TEARDOWN_MARGIN_SECONDS = 2 * KUBECTL_TIMEOUT_SECONDS + EVIDENCE_SECONDS
POLL_SECONDS = 2
STARTING = frozenset({"", "ContainerCreating", "PodInitializing"})
WORK_LIMIT_DETAIL = "the work limit ran out before the settle window ended"

NODES_NOT_READY = Outcome(
    "nodes-not-ready",
    "failed",
    "not_evaluated",
    "dependency",
    "The stand-in cluster had no Ready node for every placed host in time.",
)
CLUSTER_ERROR = Outcome(
    "cluster-error",
    "failed",
    "not_evaluated",
    "dependency",
    "kubectl failed against the stand-in cluster.",
)


@dataclass(frozen=True)
class Cluster:
    server: str
    kubeconfig: Path
    node_wait_seconds: float = 120
    settle_seconds: float = 60


STAND_IN = Cluster("https://k3s-xycar:6443", Path("/etc/sdi/k3s/kubeconfig.yaml"))


class KubectlError(Exception):
    pass


class OutOfTimeError(KubectlError):
    pass


@dataclass(frozen=True)
class Kubectl:
    binary: str
    cluster: Cluster
    scratch: Path
    deadline: float

    def __call__(self, *arguments: str, stdin: bytes = b"") -> bytes:
        command = f"kubectl {arguments[0]}"
        left = self.deadline - time.monotonic()
        if left <= 0:
            msg = f"{command}: no time left to run it"
            raise OutOfTimeError(msg)
        argv = [
            self.binary,
            "--kubeconfig",
            str(self.cluster.kubeconfig),
            "--server",
            self.cluster.server,
            "--cache-dir",
            str(self.scratch / "cache"),
            f"--request-timeout={KUBECTL_TIMEOUT_SECONDS}s",
            *arguments,
        ]
        try:
            completed = subprocess.run(  # noqa: S603
                argv,
                input=stdin,
                capture_output=True,
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
                    "HOME": str(self.scratch),
                },
                timeout=min(KUBECTL_TIMEOUT_SECONDS, left),
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            if left < KUBECTL_TIMEOUT_SECONDS:
                msg = f"{command}: its deadline passed before it answered"
                raise OutOfTimeError(msg) from error
            msg = f"{command} did not answer within {KUBECTL_TIMEOUT_SECONDS} s"
            raise KubectlError(msg) from error
        except OSError as error:
            msg = f"{command}: {type(error).__name__}"
            raise KubectlError(msg) from error
        if completed.returncode != 0:
            lines = completed.stderr.decode("utf-8", errors="replace").splitlines()
            fallback = f"{command} exited {completed.returncode}"
            text = failure_text(lines[-1] if lines else "", fallback=fallback)
            raise KubectlError(text)
        return completed.stdout


class _Kubernetes(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, frozen=True)


class _Metadata(_Kubernetes):
    labels: dict[str, str] = {}


class _Condition(_Kubernetes):
    type: str
    status: str
    reason: str = ""


class _Waiting(_Kubernetes):
    reason: str = ""


class _State(_Kubernetes):
    waiting: _Waiting | None = None


class _ContainerStatus(_Kubernetes):
    state: _State = _State()


class _Status(_Kubernetes):
    phase: str = ""
    conditions: list[_Condition] = []
    container_statuses: list[_ContainerStatus] = []


class _Object(_Kubernetes):
    metadata: _Metadata
    status: _Status = _Status()


class _List(_Kubernetes):
    items: list[_Object]


def deploy(invocation: Invocation, *, cluster: Cluster) -> Verdict:
    diagnostic = invocation.diagnostic
    inputs = _read(invocation)
    pins = pin(inputs)
    if isinstance(pins, Gap):
        return _verdict(diagnostic, deployment_result(inputs, pins.placements))
    manifest = _as_published(render(pins))
    deadline = invocation.deadline_monotonic - TEARDOWN_MARGIN_SECONDS
    namespace = f"sdi-{invocation.request.correlation.execution_id}"
    diagnostic.fact("namespace", namespace)
    binary = shutil.which("kubectl", path=os.environ.get("PATH"))
    if binary is None:
        diagnostic.line("kubectl is not on PATH")
        return Verdict(CLUSTER_ERROR)
    with tempfile.TemporaryDirectory(prefix="sdi-cd-") as scratch:
        kubectl = Kubectl(binary, cluster, Path(scratch), deadline)
        ending = replace(kubectl, deadline=invocation.deadline_monotonic)
        waited = time.monotonic()
        until = min(deadline, waited + cluster.node_wait_seconds)
        hosts = {item.host for item in pins}
        if not _nodes_ready(replace(kubectl, deadline=until), hosts):
            diagnostic.line(
                f"no Ready node for every placed host after "
                f"{round(time.monotonic() - waited)} s"
            )
            return Verdict(NODES_NOT_READY)
        diagnostic.line(f"nodes Ready after {round(time.monotonic() - waited)} s")
        try:
            kubectl("create", "namespace", namespace)
            kubectl("apply", "-n", namespace, "-f", "-", stdin=manifest_yaml(manifest))
            starting = _still_starting(kubectl, namespace, pins, diagnostic)
            left = deadline - time.monotonic()
            held = not starting and left >= cluster.settle_seconds
            if held:
                time.sleep(cluster.settle_seconds)
                diagnostic.line(f"settled for {cluster.settle_seconds:g} s")
            records = judge(pins, ending("get", "pods", "-n", namespace, "-o", "json"))
            if starting:
                diagnostic.line(
                    "the work limit ran out in the wait for Running pods: "
                    + ", ".join(sorted(starting))
                )
            elif not held:
                diagnostic.line(
                    f"the work limit left {left:.0f} s, short of the "
                    f"{cluster.settle_seconds:g} s settle window"
                )
        except KubectlError as error:
            diagnostic.line(str(error))
            return Verdict(CLUSTER_ERROR)
        finally:
            _delete(ending, namespace, diagnostic)
    if not held:
        records = tuple(_cut(item, starting) for item in records)
    return _verdict(diagnostic, deployment_result(inputs, records, manifest))


def _as_published(manifest: Manifest) -> Manifest:
    """Sort keys as the canonical-JSON evidence will, so its YAML equals the applied."""
    return cast("Manifest", json.loads(canonical_json(manifest)))


def _read(invocation: Invocation) -> CdInputs:
    paths = {item.slot: item.path for item in invocation.request.inputs}
    contents = invocation.inputs

    def document[ModelT: BaseModel](model: type[ModelT], slot: str) -> ModelT:
        content = contents[slot]
        return validate_contract(
            model, parse_json(content, paths[slot], max_bytes=len(content)), paths[slot]
        )

    return CdInputs(
        validate_contract(
            TargetExecutionProfile,
            parse_yaml(contents["target_profile"], paths["target_profile"]),
            paths["target_profile"],
        ),
        document(CompositionBlueprint, "composition_blueprint"),
        document(DeploymentSchema, "deployment_schema"),
        document(ImageBuildResult, "image_build_result"),
        tuple(
            SourceInputDigest(slot=item.slot, sha256=item.sha256)
            for item in invocation.request.inputs
        ),
    )


def _nodes_ready(kubectl: Kubectl, hosts: set[str]) -> bool:
    while not hosts <= _ready_hosts(kubectl):
        if time.monotonic() + POLL_SECONDS > kubectl.deadline:
            return False
        time.sleep(POLL_SECONDS)
    return True


def _ready_hosts(kubectl: Kubectl) -> set[str]:
    """Hosts with a Ready node; a cluster that does not answer has none yet."""
    try:
        nodes = _List.model_validate_json(
            kubectl("get", "nodes", "-l", HOST_LABEL, "-o", "json")
        ).items
    except (KubectlError, ValidationError):
        return set()
    return {
        node.metadata.labels[HOST_LABEL]
        for node in nodes
        if HOST_LABEL in node.metadata.labels
        and any(
            item.type == "Ready" and item.status == "True"
            for item in node.status.conditions
        )
    }


def _still_starting(
    kubectl: Kubectl, namespace: str, pins: Sequence[Pin], diagnostic: Diagnostic
) -> frozenset[str]:
    started = time.monotonic()
    running: set[str] = set()
    services = frozenset(item.service_id for item in pins)
    starting = services
    while True:
        try:
            pods = _List.model_validate_json(
                kubectl("get", "pods", "-n", namespace, "-o", "json")
            ).items
        except OutOfTimeError as error:
            diagnostic.line(str(error))
            return starting
        for pod in pods:
            service = pod.metadata.labels.get(SERVICE_LABEL, "")
            if pod.status.phase == "Running" and service not in running:
                running.add(service)
                elapsed = round(time.monotonic() - started)
                diagnostic.line(f"{service}: Running after {elapsed} s")
        present = {pod.metadata.labels.get(SERVICE_LABEL, "") for pod in pods}
        starting = (services - present) | {
            pod.metadata.labels.get(SERVICE_LABEL, "")
            for pod in pods
            if not _settled(pod)
        }
        if not starting or time.monotonic() + POLL_SECONDS > kubectl.deadline:
            return starting
        time.sleep(POLL_SECONDS)


def _settled(pod: _Object) -> bool:
    status = pod.status
    if status.phase in {"Running", "Succeeded", "Failed"}:
        return True
    if any(
        item.type == "PodScheduled" and item.reason == "Unschedulable"
        for item in status.conditions
    ):
        return True
    return any(
        item.state.waiting is not None and item.state.waiting.reason not in STARTING
        for item in status.container_statuses
    )


def _delete(kubectl: Kubectl, namespace: str, diagnostic: Diagnostic) -> None:
    try:
        kubectl("delete", "namespace", namespace, "--wait=false", "--ignore-not-found")
    except KubectlError as error:
        diagnostic.line(f"the namespace delete failed: {error}")


def _cut(
    record: DeployedPlacement | FailedPlacement, starting: frozenset[str]
) -> FailedPlacement:
    if isinstance(record, FailedPlacement) and record.service_id not in starting:
        return record
    return FailedPlacement.model_validate(
        record.model_dump(mode="json", exclude_none=True)
        | {"status": "failed", "detail": WORK_LIMIT_DETAIL, "k8s_reason": "other"}
    )


def _verdict(diagnostic: Diagnostic, result: DeploymentResult) -> Verdict:
    for item in result.placements:
        diagnostic.line(f"{item.service_id}: {_describe(item)}")
    return Verdict(_outcome(result), {"deployment_result": result})


def _describe(record: PlacementRecord) -> str:
    match record:
        case DeployedPlacement():
            return f"deployed on {record.node} as {record.image_id}"
        case FailedPlacement():
            return f"failed: {record.detail}"
        case UndeployedPlacement():
            return f"not evaluated: {record.reason}"


def _outcome(result: DeploymentResult) -> Outcome | Succeeded:
    match result.domain_outcome:
        case "succeeded":
            return SUCCEEDED
        case "failed":
            failed = next(
                item for item in result.placements if isinstance(item, FailedPlacement)
            )
            summary = f"{failed.service_id} failed: {failed.detail}."
            return Outcome("failed", "succeeded", "failed", "domain", summary)
        case "not_evaluated":
            gap = next(
                item
                for item in result.placements
                if isinstance(item, UndeployedPlacement) and item.reason != "blocked"
            )
            summary = f"{gap.service_id} is not evaluated: {gap.reason}."
            return Outcome(
                "not-evaluated", "succeeded", "not_evaluated", "dependency", summary
            )


main = serve("cd", partial(deploy, cluster=STAND_IN))
