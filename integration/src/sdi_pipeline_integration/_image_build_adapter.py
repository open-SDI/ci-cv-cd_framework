"""Implemented image-build Stage adapter: build each placement's recipe.

For every placement in the deployment schema, the adapter builds the Dockerfile
the placed artifact names, for every architecture the artifact declares,
against the stack's one BuildKit daemon, and pushes one image per placement to
the stack-local pipeline registry. It records each placement built, with its
image digests, or failed, with one reason from a closed set. The Domain outcome
is succeeded whenever every placement is recorded, even when nothing built.

The adapter runs on the image-build agent with only PATH and LC_ALL in its
environment, so the builder and registry addresses are constants.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

import requests

from ._adapter_kit import Outcome, Verdict, serve
from ._contracts import TargetExecutionProfile
from ._domain_contracts import (
    CompositionBlueprint,
    DeploymentSchema,
    ImageBuildResult,
)
from ._image_build_redaction import failure_detail, log_tail
from ._json_input import parse_json
from ._service_descriptions import (
    SERVICE_REPOSITORY_ROOT,
    ServiceRepositoryManifest,
    capture_service_repository,
    parse_service_repository,
)
from ._yaml_input import InputError, parse_yaml, validate_contract

if TYPE_CHECKING:
    from ._adapter_kit import Diagnostic, Invocation
    from ._domain_contracts import ServicePlacement
    from ._service_descriptions import Artifact

BUILDKIT_ADDRESS = "tcp://buildkitd:1234"
REGISTRY = "registry:5000"
REGISTRY_URL = f"http://{REGISTRY}"
# More placements than this could exceed the 32 KiB result limit.
MAX_PLACEMENTS = 48
# Builds need time to cancel before the runtime's work limit kills the group.
DEADLINE_RESERVE_SECONDS = 60
CANCEL_GRACE_SECONDS = 15
# Registry reads stop this long before the reserve ends, leaving time to publish.
PUBLISH_RESERVE_SECONDS = 15
PROBE_TIMEOUT_SECONDS = 30
REGISTRY_TIMEOUT_SECONDS = 30
POLL_SECONDS = 0.2
MANIFEST_ACCEPT = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)
ARCHITECTURES = {"amd64": ("amd64",), "arm64": ("arm64",), "noarch": ("amd64", "arm64")}

RECORDED = Outcome(
    "recorded",
    "succeeded",
    "succeeded",
    "domain",
    "Every placement was recorded as built or failed.",
)
INPUT_MISMATCH = Outcome(
    "input-mismatch",
    "failed",
    "not_evaluated",
    "input",
    "The service repository differs from the one composition read.",
)
TOO_MANY_PLACEMENTS = Outcome(
    "too-many-placements",
    "failed",
    "not_evaluated",
    "input",
    f"The deployment schema has more than {MAX_PLACEMENTS} placements.",
)
BUILDER_UNREACHABLE = Outcome(
    "builder-unreachable",
    "failed",
    "not_evaluated",
    "dependency",
    "The image builder did not answer.",
)
REGISTRY_UNREACHABLE = Outcome(
    "registry-unreachable",
    "failed",
    "not_evaluated",
    "dependency",
    "The pipeline registry did not answer.",
)
ARM64_UNAVAILABLE = Outcome(
    "arm64-unavailable",
    "failed",
    "not_evaluated",
    "dependency",
    "The image builder cannot build linux/arm64 images.",
)
# Fixed detail texts, keyed by the cause behind a failure reason.
DETAILS = {
    "unresolved-placement": "composition left the placement unresolved",
    "no-artifact": "the placed artifact is not in the service description",
    "no-recipe": "the artifact names no recipe in the service folder",
    "uncaptured-recipe": "the artifact recipe is not a file in the service folder",
    "build-failed": "the build failed",
    "no-digest": "the build reported no image digest",
    "unreadable-index": "the pushed image index could not be read back",
    "unread-index": "the work limit came before the pushed index was read back",
    "platform-mismatch": "the pushed image platforms differ from the requested ones",
    "work-limit": "the build was still running at the work limit",
}


@dataclass(frozen=True)
class Build:
    """One placement to build from its recipe."""

    service_id: str
    artifact_id: str
    recipe: PurePosixPath
    architectures: tuple[str, ...]


@dataclass
class Running:
    """One started `buildctl` client and the files it writes."""

    build: Build
    process: subprocess.Popen[bytes]
    log: Path
    metadata: Path


def _failed(service_id: str, reason: str, detail: str) -> dict[str, object]:
    return {
        "service_id": service_id,
        "status": "failed",
        "reason": reason,
        "detail": detail,
    }


def _plan(
    placement: ServicePlacement,
    artifacts: dict[str, dict[str, Artifact]],
    captured: set[str],
) -> Build | dict[str, object]:
    """Return the build for a placement, or its failed record."""
    service_id = placement.service_id
    if placement.status == "unresolved" or placement.artifact_id is None:
        return _failed(
            service_id, "unresolved-placement", DETAILS["unresolved-placement"]
        )
    artifact = artifacts.get(service_id, {}).get(placement.artifact_id)
    if artifact is None:
        return _failed(service_id, "no-recipe", DETAILS["no-artifact"])
    if artifact.dockerfile is None:
        return _failed(service_id, "no-recipe", DETAILS["no-recipe"])
    if f"{service_id}/{artifact.dockerfile}" not in captured:
        return _failed(service_id, "no-recipe", DETAILS["uncaptured-recipe"])
    architectures = sorted(
        {
            name
            for declared in artifact.architectures
            for name in ARCHITECTURES[declared]
        }
    )
    return Build(
        service_id,
        placement.artifact_id,
        PurePosixPath(artifact.dockerfile),
        tuple(architectures),
    )


def _builder_workers(buildctl: str, environment: dict[str, str]) -> str | None:
    """Return the builder's worker listing, or None when it does not answer."""
    try:
        completed = subprocess.run(  # noqa: S603
            [buildctl, "--addr", BUILDKIT_ADDRESS, "debug", "workers"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=environment,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", errors="replace")


def _registry_answers() -> bool:
    try:
        response = requests.get(f"{REGISTRY_URL}/v2/", timeout=REGISTRY_TIMEOUT_SECONDS)
    except requests.RequestException:
        return False
    return response.status_code == requests.codes.ok


def _build_services_failure(
    builds: list[Build], buildctl: str, environment: dict[str, str]
) -> Outcome | None:
    workers = _builder_workers(buildctl, environment)
    if workers is None:
        return BUILDER_UNREACHABLE
    if not _registry_answers():
        return REGISTRY_UNREACHABLE
    needs_arm64 = any("arm64" in build.architectures for build in builds)
    if needs_arm64 and "linux/arm64" not in workers:
        return ARM64_UNAVAILABLE
    return None


@dataclass(frozen=True)
class Builder:
    """What every `buildctl` client of one run shares."""

    buildctl: str
    repository: Path
    scratch: Path
    execution_id: str
    environment: dict[str, str]

    def start(self, build: Build) -> Running:
        """Start one placement's build, logging to the scratch directory."""
        log = self.scratch / f"{build.service_id}.log"
        metadata = self.scratch / f"{build.service_id}.metadata.json"
        with log.open("wb") as stream:
            # Same process group as the adapter, so the runtime's group kill
            # still reaches every client on a hard timeout.
            process = subprocess.Popen(  # noqa: S603
                self._command(build, metadata),
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                env=self.environment,
                cwd=self.scratch,
            )
        return Running(build, process, log, metadata)

    def _command(self, build: Build, metadata: Path) -> list[str]:
        name = (
            f"{REGISTRY}/sdi/{build.service_id}:{build.artifact_id}-{self.execution_id}"
        )
        platforms = ",".join(f"linux/{item}" for item in build.architectures)
        # The service folder is the build context; the recipe may sit below it.
        context = self.repository / build.service_id
        return [
            self.buildctl,
            "--addr",
            BUILDKIT_ADDRESS,
            "build",
            "--frontend",
            "dockerfile.v0",
            "--local",
            f"context={context}",
            "--local",
            f"dockerfile={context / build.recipe.parent}",
            "--opt",
            f"filename={build.recipe.name}",
            "--opt",
            f"platform={platforms}",
            "--output",
            f"type=image,name={name},push=true,registry.insecure=true",
            "--metadata-file",
            str(metadata),
            "--progress=plain",
        ]


def _built_digest(running: Running) -> str | None:
    try:
        metadata: object = json.loads(running.metadata.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(metadata, dict):
        return None
    digest = cast("dict[str, object]", metadata).get("containerimage.digest")
    return digest if isinstance(digest, str) else None


def _index_platforms(
    service_id: str, digest: str, timeout: float
) -> dict[str, str] | None:
    """Read a pushed index back and return its digest per architecture."""
    try:
        response = requests.get(
            f"{REGISTRY_URL}/v2/sdi/{service_id}/manifests/{digest}",
            headers={"Accept": MANIFEST_ACCEPT},
            timeout=timeout,
        )
    except requests.RequestException:
        return None
    body = response.content
    if (
        response.status_code != requests.codes.ok
        or f"sha256:{hashlib.sha256(body).hexdigest()}" != digest
    ):
        return None
    try:
        manifests = json.loads(body)["manifests"]
        platforms: dict[str, str] = {}
        for entry in manifests:
            platform = entry["platform"]
            # BuildKit may list attestation manifests as unknown/unknown.
            if platform["os"] == "linux":
                platforms[platform["architecture"]] = entry["digest"]
    except (ValueError, KeyError, TypeError):
        return None
    return platforms


def _finished_record(running: Running, read_cutoff: float) -> dict[str, object]:
    """Return the record of a build whose client has exited."""
    build = running.build
    if running.process.returncode != 0:
        return _failed(
            build.service_id,
            "build-failed",
            failure_detail(running.log, fallback=DETAILS["build-failed"]),
        )
    digest = _built_digest(running)
    if digest is None:
        return _failed(build.service_id, "build-failed", DETAILS["no-digest"])
    if len(build.architectures) == 1:
        platforms = {build.architectures[0]: digest}
    else:
        timeout = min(REGISTRY_TIMEOUT_SECONDS, read_cutoff - time.monotonic())
        if timeout <= 0:
            return _failed(build.service_id, "work-limit", DETAILS["unread-index"])
        indexed = _index_platforms(build.service_id, digest, timeout)
        if indexed is None:
            return _failed(
                build.service_id, "build-failed", DETAILS["unreadable-index"]
            )
        platforms = indexed
    if sorted(platforms) != list(build.architectures):
        return _failed(build.service_id, "build-failed", DETAILS["platform-mismatch"])
    return {
        "service_id": build.service_id,
        "status": "built",
        "repository": f"sdi/{build.service_id}",
        "digest": digest,
        "platforms": [
            {"architecture": name, "digest": platforms[name]}
            for name in build.architectures
        ],
    }


def _stop(running: list[Running]) -> None:
    """Signal every client still running, then reap them all."""
    for item in running:
        if item.process.poll() is None:
            item.process.send_signal(signal.SIGTERM)
    grace_ends = time.monotonic() + CANCEL_GRACE_SECONDS
    for item in running:
        try:
            item.process.wait(timeout=max(0.0, grace_ends - time.monotonic()))
        except subprocess.TimeoutExpired:
            item.process.kill()
            item.process.wait()


def _run_builds(
    builds: list[Build],
    *,
    deadline: float,
    builder: Builder,
) -> tuple[dict[str, dict[str, object]], list[Running]]:
    """Run every build at once and record each until the deadline."""
    records: dict[str, dict[str, object]] = {}
    running: list[Running] = []
    # Reads may run past the deadline, but never into the cancel grace.
    read_cutoff = (
        deadline
        + DEADLINE_RESERVE_SECONDS
        - CANCEL_GRACE_SECONDS
        - PUBLISH_RESERVE_SECONDS
    )
    if time.monotonic() >= deadline:
        # A work limit inside the reserve leaves no time to build at all.
        for build in builds:
            records[build.service_id] = _failed(
                build.service_id, "work-limit", DETAILS["work-limit"]
            )
        return records, running
    try:
        # One at a time, so a failed start still stops the ones before it.
        for build in builds:
            running.append(builder.start(build))  # noqa: PERF401
        pending = list(running)
        while pending and time.monotonic() < deadline:
            for item in list(pending):
                if item.process.poll() is not None:
                    pending.remove(item)
                    records[item.build.service_id] = _finished_record(item, read_cutoff)
            if pending:
                time.sleep(POLL_SECONDS)
        # A client that finished before it was signalled still counts.
        finished = [item for item in pending if item.process.poll() is not None]
        unfinished = [item for item in pending if item not in finished]
        _stop(unfinished)
        for item in finished:
            records[item.build.service_id] = _finished_record(item, read_cutoff)
        for item in unfinished:
            records[item.build.service_id] = _failed(
                item.build.service_id, "work-limit", DETAILS["work-limit"]
            )
    finally:
        _stop(running)
    return records, running


def _diagnose(
    diagnostic: Diagnostic, records: list[dict[str, object]], logs: dict[str, Path]
) -> None:
    for record in records:
        if record["status"] == "built":
            diagnostic.line(f"{record['service_id']}: built {record['digest']}")
        else:
            diagnostic.line(
                f"{record['service_id']}: {record['reason']}: {record['detail']}"
            )
    failed = [
        (str(record["service_id"]), logs[str(record["service_id"])])
        for record in records
        if record["status"] == "failed" and record["service_id"] in logs
    ]
    if not failed:
        return
    for service_id, log in failed:
        title = f"{service_id} build log tail"
        room = diagnostic.block_room(RECORDED, title, shares=len(failed))
        if room <= 0:
            diagnostic.mark_cut()
            continue
        tail = log_tail(log, max_bytes=room)
        diagnostic.block(title, tail.lines, cut=tail.cut)


def _merge(
    plans: list[Build | dict[str, object]], built: dict[str, dict[str, object]]
) -> list[dict[str, object]]:
    """Return one record per placement, in deployment-schema order."""
    return [
        built[plan.service_id] if isinstance(plan, Build) else plan for plan in plans
    ]


def _build_environment(scratch: Path) -> dict[str, str]:
    """Return buildctl's whole environment: no inherited credentials."""
    config = scratch / "docker-config"
    config.mkdir()
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "HOME": str(scratch),
        "DOCKER_CONFIG": str(config),
    }


def _image_build(invocation: Invocation) -> Verdict:
    request = invocation.request
    declared = {item.slot: item for item in request.inputs}
    contents = invocation.inputs
    paths = {slot: item.path for slot, item in declared.items()}
    manifest = validate_contract(
        ServiceRepositoryManifest,
        parse_json(
            contents["service_repository"],
            paths["service_repository"],
            max_bytes=len(contents["service_repository"]),
        ),
        paths["service_repository"],
    )
    repository_root = invocation.input_root / SERVICE_REPOSITORY_ROOT
    snapshot = capture_service_repository(repository_root)
    if snapshot.manifest != manifest:
        msg = "copied service folders do not match the service-repository manifest"
        raise InputError(msg)
    validate_contract(
        TargetExecutionProfile,
        parse_yaml(contents["target_profile"], paths["target_profile"]),
        paths["target_profile"],
    )
    validate_contract(
        CompositionBlueprint,
        parse_json(
            contents["composition_blueprint"],
            paths["composition_blueprint"],
            max_bytes=len(contents["composition_blueprint"]),
        ),
        paths["composition_blueprint"],
    )
    deployment = validate_contract(
        DeploymentSchema,
        parse_json(
            contents["deployment_schema"],
            paths["deployment_schema"],
            max_bytes=len(contents["deployment_schema"]),
        ),
        paths["deployment_schema"],
    )
    composed = {item.slot: item.sha256 for item in deployment.source_inputs}
    if composed.get("service_repository") != declared["service_repository"].sha256:
        return Verdict(INPUT_MISMATCH)
    if len(deployment.placements) > MAX_PLACEMENTS:
        return Verdict(TOO_MANY_PLACEMENTS)

    artifacts = {
        file.description.service_id: file.description.artifacts or {}
        for file in parse_service_repository(snapshot.files)
    }
    plans = [
        _plan(placement, artifacts, set(snapshot.files))
        for placement in deployment.placements
    ]
    builds = [plan for plan in plans if isinstance(plan, Build)]
    if builds:
        with tempfile.TemporaryDirectory(prefix="sdi-image-build-") as directory:
            scratch = Path(directory)
            environment = _build_environment(scratch)
            buildctl = shutil.which("buildctl", path=environment["PATH"])
            if buildctl is None:
                return Verdict(BUILDER_UNREACHABLE)
            failure = _build_services_failure(builds, buildctl, environment)
            if failure is not None:
                return Verdict(failure)
            built, running = _run_builds(
                builds,
                deadline=invocation.deadline_monotonic - DEADLINE_RESERVE_SECONDS,
                builder=Builder(
                    buildctl,
                    repository_root,
                    scratch,
                    request.correlation.execution_id,
                    environment,
                ),
            )
            records = _merge(plans, built)
            _diagnose(
                invocation.diagnostic,
                records,
                {item.build.service_id: item.log for item in running},
            )
    else:
        records = _merge(plans, {})
        _diagnose(invocation.diagnostic, records, {})
    identifier = (
        f"{deployment.scenario_id}-{deployment.testcase_id}-"
        f"{deployment.combination_id}-image-build"
    ).lower()
    result = validate_contract(
        ImageBuildResult,
        {
            "schema_version": "sdi.image-build-result/v1",
            "evidence_basis": "implemented",
            "image_build_result_id": identifier,
            "blueprint_id": deployment.blueprint_id,
            "deployment_schema_id": deployment.deployment_schema_id,
            "scenario_id": deployment.scenario_id,
            "testcase_id": deployment.testcase_id,
            "combination_id": deployment.combination_id,
            "profile_id": deployment.profile_id,
            "source_inputs": [
                {"slot": item.slot, "sha256": item.sha256} for item in request.inputs
            ],
            "registry": REGISTRY,
            "images": records,
        },
        "image-build-result.json",
    )
    return Verdict(RECORDED, {"image_build_result": result})


main = serve("image_build", _image_build)
