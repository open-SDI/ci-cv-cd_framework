"""Recovery manifest and reviewed installation authority."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Self, cast
from urllib.parse import urlsplit

from pydantic import (
    AwareDatetime,
    Field,
    PositiveInt,
    StringConstraints,
    ValidationError,
    model_validator,
)

from ._contracts import ContractModel
from ._yaml_input import parse_yaml

RECOVERY_MANIFEST_SCHEMA_VERSION = "sdi.scaffold-recovery-manifest/v1"
INSTALLATION_CONTRACT_VERSION = "sdi.scaffold-installation/v1"

Sha256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
GitCommit = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{40}$")]
PinnedImage = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^[a-z0-9./_-]+(?::[A-Za-z0-9_.-]+)?@sha256:[0-9a-f]{64}$",
    ),
]


class InstallationPins(ContractModel):
    """Exact reconstructable software and workflow inputs."""

    jenkins_controller_image: PinnedImage
    jenkins_plugin_count: PositiveInt
    jenkins_plugin_lock_sha256: Sha256
    python_runtime_image: PinnedImage
    uv_image: PinnedImage
    jenkins_remoting_image: PinnedImage
    composition_adapter_image: PinnedImage
    image_build_adapter_image: PinnedImage
    cv_adapter_image: PinnedImage
    cd_adapter_image: PinnedImage
    python_version: str = Field(strict=True, min_length=1)
    uv_version: str = Field(strict=True, min_length=1)
    github_runner_version: str = Field(strict=True, min_length=1)
    github_runner_sha256: Sha256
    checkout_action_commit: GitCommit
    setup_uv_action_commit: GitCommit
    upload_artifact_action_commit: GitCommit


class ArchiveFacts(ContractModel):
    """Content identity for one complete controller-volume archive."""

    size_bytes: PositiveInt
    sha256: Sha256


class RecoveryManifest(ContractModel):
    """Sanitized reconstruction facts for one cold Jenkins snapshot."""

    schema_version: Literal["sdi.scaffold-recovery-manifest/v1"]
    repository_commit: GitCommit
    created_at: AwareDatetime
    archive: ArchiveFacts
    pins: InstallationPins


class InstallationAuthority(ContractModel):
    """Reviewed non-secret host support and exact installation pins."""

    contract_version: Literal["sdi.scaffold-installation/v1"]
    supported_ubuntu_lts: tuple[str, ...] = Field(min_length=1)
    docker_engine_min_major: PositiveInt
    docker_compose_min_major: PositiveInt
    git_min_major: PositiveInt
    required_https_urls: tuple[str, ...] = Field(min_length=1)
    pins: InstallationPins

    @model_validator(mode="after")
    def validate_public_endpoints(self) -> Self:
        for value in self.required_https_urls:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or parsed.hostname is None
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                msg = "installation authority contains an invalid HTTPS endpoint"
                raise ValueError(msg)
        return self

    def recovery_manifest(
        self, *, repository_commit: str, archive_path: Path
    ) -> RecoveryManifest:
        """Build a manifest from observed immutable archive facts."""
        return RecoveryManifest(
            schema_version=RECOVERY_MANIFEST_SCHEMA_VERSION,
            repository_commit=repository_commit,
            created_at=datetime.now(UTC),
            archive=_archive_facts(archive_path),
            pins=self.pins,
        )


_EXPECTED_KEYS = {
    "SDI_INSTALLATION_CONTRACT_VERSION",
    "SDI_SUPPORTED_UBUNTU_LTS",
    "SDI_DOCKER_ENGINE_MIN_MAJOR",
    "SDI_DOCKER_COMPOSE_MIN_MAJOR",
    "SDI_GIT_MIN_MAJOR",
    "SDI_REQUIRED_HTTPS_URLS",
    "SDI_JENKINS_CONTROLLER_IMAGE",
    "SDI_JENKINS_PLUGIN_COUNT",
    "SDI_JENKINS_PLUGIN_LOCK_SHA256",
    "SDI_PYTHON_RUNTIME_IMAGE",
    "SDI_UV_IMAGE",
    "SDI_JENKINS_REMOTING_IMAGE",
    "SDI_COMPOSITION_ADAPTER_IMAGE",
    "SDI_IMAGE_BUILD_ADAPTER_IMAGE",
    "SDI_CV_ADAPTER_IMAGE",
    "SDI_CD_ADAPTER_IMAGE",
    "SDI_PYTHON_VERSION",
    "SDI_UV_VERSION",
    "SDI_GITHUB_RUNNER_VERSION",
    "SDI_GITHUB_RUNNER_SHA256",
    "SDI_GITHUB_RUNNER_LABEL",
    "SDI_CHECKOUT_ACTION_COMMIT",
    "SDI_SETUP_UV_ACTION_COMMIT",
    "SDI_UPLOAD_ARTIFACT_ACTION_COMMIT",
}


def _archive_facts(path: Path) -> ArchiveFacts:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return ArchiveFacts(size_bytes=path.stat().st_size, sha256=digest.hexdigest())


def _read_authority_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in _EXPECTED_KEYS or not value:
            msg = "installation authority contains an invalid entry"
            raise ValueError(msg)
        if key in values:
            msg = "installation authority contains a duplicate entry"
            raise ValueError(msg)
        values[key] = value
    if values.keys() != _EXPECTED_KEYS:
        msg = "installation authority keys do not match the reviewed contract"
        raise ValueError(msg)
    return values


def load_installation_authority(path: Path) -> InstallationAuthority:
    """Load the strict reviewed non-secret installation authority."""
    values = _read_authority_values(path)
    if values["SDI_INSTALLATION_CONTRACT_VERSION"] != INSTALLATION_CONTRACT_VERSION:
        msg = "installation authority uses an unsupported contract version"
        raise ValueError(msg)
    return InstallationAuthority(
        contract_version=INSTALLATION_CONTRACT_VERSION,
        supported_ubuntu_lts=tuple(values["SDI_SUPPORTED_UBUNTU_LTS"].split(",")),
        docker_engine_min_major=int(values["SDI_DOCKER_ENGINE_MIN_MAJOR"]),
        docker_compose_min_major=int(values["SDI_DOCKER_COMPOSE_MIN_MAJOR"]),
        git_min_major=int(values["SDI_GIT_MIN_MAJOR"]),
        required_https_urls=tuple(values["SDI_REQUIRED_HTTPS_URLS"].split(",")),
        pins=InstallationPins(
            jenkins_controller_image=values["SDI_JENKINS_CONTROLLER_IMAGE"],
            jenkins_plugin_count=int(values["SDI_JENKINS_PLUGIN_COUNT"]),
            jenkins_plugin_lock_sha256=values["SDI_JENKINS_PLUGIN_LOCK_SHA256"],
            python_runtime_image=values["SDI_PYTHON_RUNTIME_IMAGE"],
            uv_image=values["SDI_UV_IMAGE"],
            jenkins_remoting_image=values["SDI_JENKINS_REMOTING_IMAGE"],
            composition_adapter_image=values["SDI_COMPOSITION_ADAPTER_IMAGE"],
            image_build_adapter_image=values["SDI_IMAGE_BUILD_ADAPTER_IMAGE"],
            cv_adapter_image=values["SDI_CV_ADAPTER_IMAGE"],
            cd_adapter_image=values["SDI_CD_ADAPTER_IMAGE"],
            python_version=values["SDI_PYTHON_VERSION"],
            uv_version=values["SDI_UV_VERSION"],
            github_runner_version=values["SDI_GITHUB_RUNNER_VERSION"],
            github_runner_sha256=values["SDI_GITHUB_RUNNER_SHA256"],
            checkout_action_commit=values["SDI_CHECKOUT_ACTION_COMMIT"],
            setup_uv_action_commit=values["SDI_SETUP_UV_ACTION_COMMIT"],
            upload_artifact_action_commit=values["SDI_UPLOAD_ARTIFACT_ACTION_COMMIT"],
        ),
    )


def _require_single_assignment(
    path: Path, *, prefix: str, expected: str, label: str
) -> None:
    assignments = [
        line.split("#", maxsplit=1)[0].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.split("#", maxsplit=1)[0]
        .strip()
        .casefold()
        .startswith(prefix.casefold())
    ]
    if assignments != [expected]:
        msg = f"{label} does not match the reviewed installation authority"
        raise ValueError(msg)


def _docker_instructions(path: Path, name: str) -> list[str]:
    instructions: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        keyword, *arguments = stripped.split(maxsplit=1)
        if keyword.casefold() == name.casefold():
            instructions.append(arguments[0] if arguments else "")
    return instructions


def _mapping(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        msg = f"{label} is not a mapping"
        raise TypeError(msg)
    mapping = cast("dict[object, object]", value)
    if not all(isinstance(key, str) for key in mapping):
        msg = f"{label} contains a non-string key"
        raise TypeError(msg)
    return cast("dict[str, object]", mapping)


def _sequence(value: object, *, label: str) -> list[object]:
    if not isinstance(value, list):
        msg = f"{label} is not a sequence"
        raise TypeError(msg)
    return cast("list[object]", value)


def _validate_agent_pins(repository: Path, pins: InstallationPins) -> None:
    agents = repository / "deployment/jenkins/agents/Dockerfile"
    for label, name, image in (
        ("Python runtime pin", "RUNTIME_IMAGE", pins.python_runtime_image),
        ("uv image pin", "UV_IMAGE", pins.uv_image),
        ("Jenkins Remoting pin", "REMOTING_IMAGE", pins.jenkins_remoting_image),
    ):
        arguments = [
            value
            for value in _docker_instructions(agents, "ARG")
            if value.partition("=")[0] == name
        ]
        if arguments != [f"{name}={image}"]:
            msg = f"{label} does not match the reviewed installation authority"
            raise ValueError(msg)
    from_directives = _docker_instructions(agents, "FROM")
    if from_directives != [
        "${RUNTIME_IMAGE} AS runtime",
        "${UV_IMAGE} AS uv",
        "${REMOTING_IMAGE} AS remoting",
        "runtime AS tooling",
        "runtime",
    ]:
        msg = "agent build stages do not consume the reviewed image pins"
        raise ValueError(msg)

    adapters = repository / "deployment/jenkins/adapters"
    for filename, image in (
        ("composition-fixture-v1.yaml", pins.composition_adapter_image),
        ("image-build-fixture-v1.yaml", pins.image_build_adapter_image),
        ("cv-fixture-v1.yaml", pins.cv_adapter_image),
        ("cd-fixture-v1.yaml", pins.cd_adapter_image),
    ):
        descriptor = parse_yaml(
            (adapters / filename).read_bytes(),
            f"deployment/jenkins/adapters/{filename}",
        )
        if descriptor.get("image") != image:
            msg = f"{filename} image pin does not match the installation authority"
            raise ValueError(msg)

    compose = repository / "deployment/jenkins/compose.yaml"
    compose_document = parse_yaml(
        compose.read_bytes(), "deployment/jenkins/compose.yaml"
    )
    services = _mapping(compose_document.get("services"), label="Compose services")
    integration_service = _mapping(
        services.get("integration"), label="Compose integration service"
    )
    integration_build = _mapping(
        integration_service.get("build"), label="Compose integration build"
    )
    integration_args = _mapping(
        integration_build.get("args"), label="Compose integration build arguments"
    )
    if integration_args.get("RUNTIME_IMAGE") != pins.python_runtime_image:
        msg = "integration agent runtime pin does not match the installation authority"
        raise ValueError(msg)


def _validate_workflow_pins(repository: Path, pins: InstallationPins) -> None:
    workflow = repository / ".github/workflows/pipeline-integration.yml"
    workflow_document = parse_yaml(
        workflow.read_bytes(), ".github/workflows/pipeline-integration.yml"
    )
    jobs = _mapping(workflow_document.get("jobs"), label="workflow jobs")
    handoff = _mapping(jobs.get("handoff"), label="workflow handoff job")
    raw_steps = _sequence(handoff.get("steps"), label="workflow handoff steps")
    steps = [_mapping(value, label="workflow step") for value in raw_steps]
    uses_values = [
        str(step["uses"]) for step in steps if isinstance(step.get("uses"), str)
    ]
    for label, action, commit in (
        ("checkout action pin", "actions/checkout", pins.checkout_action_commit),
        ("setup-uv action pin", "astral-sh/setup-uv", pins.setup_uv_action_commit),
        (
            "upload-artifact action pin",
            "actions/upload-artifact",
            pins.upload_artifact_action_commit,
        ),
    ):
        matches = [value for value in uses_values if value.startswith(f"{action}@")]
        if matches != [f"{action}@{commit}"]:
            msg = f"{label} does not match the reviewed installation authority"
            raise ValueError(msg)
    setup_steps = [
        step
        for step in steps
        if step.get("uses") == f"astral-sh/setup-uv@{pins.setup_uv_action_commit}"
    ]
    if len(setup_steps) != 1:
        msg = "workflow uv pin does not match the reviewed installation authority"
        raise ValueError(msg)
    setup_with = _mapping(setup_steps[0].get("with"), label="workflow setup-uv inputs")
    if str(setup_with.get("version")) != pins.uv_version:
        msg = "workflow uv pin does not match the reviewed installation authority"
        raise ValueError(msg)


def validate_repository_authority(
    repository: Path, authority: InstallationAuthority
) -> None:
    """Prove that every tracked installation source agrees with the authority."""
    pins = authority.pins
    controller = repository / "deployment/jenkins/controller/Dockerfile"
    controller_from = _docker_instructions(controller, "FROM")
    if controller_from != [pins.jenkins_controller_image]:
        msg = "controller pin does not match the reviewed installation authority"
        raise ValueError(msg)

    plugin_lock = repository / "deployment/jenkins/controller/plugins.txt"
    plugin_lines = [line for line in plugin_lock.read_text().splitlines() if line]
    if len(plugin_lines) != pins.jenkins_plugin_count:
        msg = "plugin count does not match the reviewed installation authority"
        raise ValueError(msg)
    if hashlib.sha256(plugin_lock.read_bytes()).hexdigest() != (
        pins.jenkins_plugin_lock_sha256
    ):
        msg = "plugin lock does not match the reviewed installation authority"
        raise ValueError(msg)

    _validate_agent_pins(repository, pins)
    python_version = (repository / "integration/.python-version").read_text().strip()
    if python_version != pins.python_version:
        msg = "Python version does not match the installation authority"
        raise ValueError(msg)

    _validate_workflow_pins(repository, pins)

    runner = repository / "deployment/github-runner/bin/runner"
    _require_single_assignment(
        runner,
        prefix="RUNNER_VERSION=",
        expected=f'RUNNER_VERSION="{pins.github_runner_version}"',
        label="runner version pin",
    )
    _require_single_assignment(
        runner,
        prefix="RUNNER_SHA256=",
        expected=f'RUNNER_SHA256="{pins.github_runner_sha256}"',
        label="runner digest pin",
    )
    _require_single_assignment(
        runner,
        prefix="--labels ",
        expected="--labels sdi-jenkins-handoff \\",
        label="runner label pin",
    )


def _write_manifest(
    authority: InstallationAuthority,
    *,
    archive: Path,
    repository_commit: str,
    output: Path,
) -> None:
    manifest = authority.recovery_manifest(
        repository_commit=repository_commit, archive_path=archive
    )
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(manifest.model_dump_json(exclude_none=True))
        stream.write("\n")


def _check_manifest(
    authority: InstallationAuthority,
    *,
    archive: Path,
    repository_commit: str,
    manifest_path: Path,
) -> None:
    manifest = RecoveryManifest.model_validate_json(manifest_path.read_bytes())
    expected = authority.recovery_manifest(
        repository_commit=repository_commit, archive_path=archive
    )
    if (
        manifest.repository_commit != repository_commit
        or manifest.pins != authority.pins
    ):
        msg = "recovery manifest does not match this approved installation"
        raise ValueError(msg)
    if manifest.archive != expected.archive:
        msg = "archive does not match its recovery manifest"
        raise ValueError(msg)


_CONTROLLER_CREDENTIAL_KEYS = (
    "JENKINS_ADMIN_PASSWORD_FILE",
    "JENKINS_HANDOFF_PASSWORD_FILE",
)
_AGENT_CREDENTIAL_KEYS = (
    "JENKINS_INTEGRATION_AGENT_SECRET_FILE",
    "JENKINS_CI_AGENT_SECRET_FILE",
    "JENKINS_IMAGE_BUILD_AGENT_SECRET_FILE",
    "JENKINS_CV_AGENT_SECRET_FILE",
    "JENKINS_CD_AGENT_SECRET_FILE",
)
_CREDENTIAL_KEYS = _CONTROLLER_CREDENTIAL_KEYS + _AGENT_CREDENTIAL_KEYS


def _read_credential_paths(config: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for line in config.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key in _CREDENTIAL_KEYS:
            if key in paths:
                msg = "controller configuration repeats a credential file setting"
                raise ValueError(msg)
            paths[key] = Path(value)
    if set(paths) != set(_CREDENTIAL_KEYS):
        msg = "controller configuration does not declare every credential file"
        raise ValueError(msg)
    return paths


def _validate_credential_targets(paths: dict[str, Path]) -> None:
    repository = Path(__file__).parents[3]
    resolved_paths: set[Path] = set()
    for key, path in paths.items():
        if not path.is_absolute():
            msg = "credential file paths must be absolute"
            raise ValueError(msg)
        resolved = path.resolve(strict=False)
        if resolved == repository or repository in resolved.parents:
            msg = "credential files must be outside the repository"
            raise ValueError(msg)
        if resolved in resolved_paths:
            msg = "credential file paths must be distinct"
            raise ValueError(msg)
        resolved_paths.add(resolved)
        if key in _AGENT_CREDENTIAL_KEYS:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                msg = "agent credential targets must be regular files"
                raise ValueError(msg)
            parent = path.parent
            if not parent.is_dir() or not os.access(parent, os.W_OK):
                msg = "agent credential target directories must be writable"
                raise ValueError(msg)


def _validate_controller_credentials(
    paths: dict[str, Path], *, snapshot_created_at: datetime
) -> None:
    values: dict[str, str] = {}
    for key in _CONTROLLER_CREDENTIAL_KEYS:
        path = paths[key]
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        if modified_at <= snapshot_created_at:
            msg = "controller credential files must be staged after the snapshot"
            raise ValueError(msg)
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            msg = "controller credential files must be nonempty"
            raise ValueError(msg)
        values[key] = value

    if values["JENKINS_ADMIN_PASSWORD_FILE"] == values["JENKINS_HANDOFF_PASSWORD_FILE"]:
        msg = "fresh controller identity credentials must differ"
        raise ValueError(msg)


def _check_fresh_credentials(*, config: Path, manifest_path: Path) -> None:
    manifest = RecoveryManifest.model_validate_json(manifest_path.read_bytes())
    paths = _read_credential_paths(config)
    _validate_credential_targets(paths)
    _validate_controller_credentials(paths, snapshot_created_at=manifest.created_at)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    authority = commands.add_parser("authority")
    authority.add_argument("--repository", type=Path, required=True)
    authority.add_argument("--installation", type=Path, required=True)
    for command in ("write", "check"):
        operation = commands.add_parser(command)
        operation.add_argument("--installation", type=Path, required=True)
        operation.add_argument("--archive", type=Path, required=True)
        operation.add_argument("--repository-commit", required=True)
        operation.add_argument("--manifest", type=Path, required=True)
    credentials = commands.add_parser("credentials")
    credentials.add_argument("--config", type=Path, required=True)
    credentials.add_argument("--installation", type=Path, required=True)
    credentials.add_argument("--manifest", type=Path, required=True)
    return parser


def main() -> int:
    """Validate installation authority or write/check a recovery manifest."""
    arguments = _parser().parse_args()
    try:
        authority = load_installation_authority(arguments.installation)
        if arguments.command == "authority":
            validate_repository_authority(arguments.repository, authority)
        elif arguments.command == "write":
            _write_manifest(
                authority,
                archive=arguments.archive,
                repository_commit=arguments.repository_commit,
                output=arguments.manifest,
            )
        elif arguments.command == "check":
            _check_manifest(
                authority,
                archive=arguments.archive,
                repository_commit=arguments.repository_commit,
                manifest_path=arguments.manifest,
            )
        else:
            _check_fresh_credentials(
                config=arguments.config, manifest_path=arguments.manifest
            )
    except (OSError, ValueError, ValidationError) as error:
        sys.stderr.write(f"recovery-contract: {error}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
