"""Strict reading of SDI service descriptions from a supplied directory."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import (
    Field,
    NonNegativeInt,
    StringConstraints,
    ValidationError,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema  # noqa: TC002

from ._contracts import (
    Baseline,
    ContractModel,
    NonBlank,
    Provenance,
    RoleMaps,
    Slug,
    reject_explicit_nulls,
)
from ._stage_contracts import Sha256  # noqa: TC001
from ._yaml_input import InputError, parse_front_matter, validate_contract

if TYPE_CHECKING:
    from collections.abc import Mapping

SERVICE_DESCRIPTION_SCHEMA_VERSION = "sdi.service-description/v1"
# The directory under a Stage's input root that holds the copied service folders.
SERVICE_REPOSITORY_ROOT = "service-repository"
DESCRIPTION_FILENAME = "SDI.md"
MAX_REPOSITORY_FILES = 256
MAX_REPOSITORY_BYTES = 1024 * 1024
# One file or folder name inside a service folder; it excludes dotfiles.
NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]*"

type Architecture = Literal["amd64", "arm64", "noarch"]


class Implementation(ContractModel):
    """Carried identity of the upstream implementation."""

    name: NonBlank
    version: NonBlank | SkipJsonSchema[None] = None
    repository: NonBlank
    revision: NonBlank

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require an unknown version to be omitted rather than null."""
        return reject_explicit_nulls(data, ("version",))


# A Debian package with its version pin, such as ros-jazzy-nav2-bringup=1.3.13-1*.
PinnedPackage = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9+.-]+=\S+$")]


class AptRoute(ContractModel):
    """Debian packages installed with their carried version pins."""

    kind: Literal["apt"]
    packages: Annotated[list[PinnedPackage], Field(min_length=1)]


class ImageRoute(ContractModel):
    """A container image reference."""

    kind: Literal["image"]
    reference: NonBlank


class SourceRoute(ContractModel):
    """A source checkout built on the host."""

    kind: Literal["source"]
    repository: NonBlank
    revision: NonBlank


Route = Annotated[AptRoute | ImageRoute | SourceRoute, Field(discriminator="kind")]
RecipePath = Annotated[
    str,
    StringConstraints(strict=True, pattern=rf"^{NAME_PATTERN}(?:/{NAME_PATTERN})*$"),
]


class Artifact(ContractModel):
    """One fixed deployable variant of a service."""

    architectures: Annotated[list[Architecture], Field(min_length=1)]
    host_requirement: Baseline | SkipJsonSchema[None] = None
    route: Route
    # The recipe, relative to the service folder that is also its build context.
    dockerfile: RecipePath | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require absent optional facts to be omitted rather than null."""
        return reject_explicit_nulls(data, ("host_requirement", "dockerfile"))


class Frames(ContractModel):
    """Carried coordinate frame names."""

    global_frame: NonBlank | SkipJsonSchema[None] = Field(default=None, alias="global")
    odometry: NonBlank | SkipJsonSchema[None] = None
    robot_base: NonBlank | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require unknown frames to be omitted rather than null."""
        return reject_explicit_nulls(data, ("global", "odometry", "robot_base"))


class ServiceDescription(RoleMaps):
    """Front matter of one mobility service description."""

    schema_version: Literal["sdi.service-description/v1"]
    service_id: Slug
    title: NonBlank
    provenance: Provenance
    implementation: Implementation | SkipJsonSchema[None] = None
    devices: list[NonBlank] | SkipJsonSchema[None] = None
    depends_on: list[Slug] | SkipJsonSchema[None] = None
    artifacts: (
        Annotated[dict[Slug, Artifact], Field(min_length=1)] | SkipJsonSchema[None]
    ) = None
    frames: Frames | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require unknown facts to be omitted rather than null."""
        return reject_explicit_nulls(
            data,
            (
                "implementation",
                "devices",
                "depends_on",
                "artifacts",
                "frames",
            ),
        )


@dataclass(frozen=True)
class DescriptionFile:
    """One validated description with its exact-byte identity."""

    path: str
    sha256: str
    description: ServiceDescription
    body: str


def _parse(path: str, raw: bytes) -> DescriptionFile:
    front_matter, body = parse_front_matter(raw, path)
    description = validate_contract(ServiceDescription, front_matter, path)
    folder = PurePosixPath(path).parts[0]
    if description.service_id != folder:
        msg = (
            f"{path}: service_id {description.service_id} "
            f"does not match its folder {folder}"
        )
        raise InputError(msg)
    return DescriptionFile(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        description=description,
        body=body,
    )


def _check_cross_file(files: list[DescriptionFile]) -> None:
    # Each service_id equals its folder name, so the ids are already unique.
    by_id = {file.description.service_id: file for file in files}
    graph: TopologicalSorter[str] = TopologicalSorter()
    for file in files:
        dependencies = file.description.depends_on or []
        for dependency in dependencies:
            if dependency not in by_id:
                msg = f"{file.path}: depends_on names unknown service {dependency}"
                raise InputError(msg)
        graph.add(file.description.service_id, *dependencies)
    try:
        graph.prepare()
    except CycleError as error:
        msg = f"depends_on cycle: {' -> '.join(error.args[1])}"
        raise InputError(msg) from error


class ManifestFile(ContractModel):
    """One service-folder file copied into a Stage's inputs."""

    path: Annotated[
        str,
        Field(strict=True, pattern=rf"^{NAME_PATTERN}(?:/{NAME_PATTERN})+$"),
    ]
    byte_size: NonNegativeInt
    sha256: Sha256


class ServiceRepositoryManifest(ContractModel):
    """The exact service-folder files a Stage receives from the service repository."""

    schema_version: Literal["sdi.service-repository-manifest/v1"]
    files: Annotated[
        list[ManifestFile], Field(min_length=1, max_length=MAX_REPOSITORY_FILES)
    ]

    @model_validator(mode="after")
    def validate_files(self) -> ServiceRepositoryManifest:
        paths = [item.path for item in self.files]
        if paths != sorted(set(paths)):
            msg = "manifest paths must be unique and sorted"
            raise ValueError(msg)
        if sum(item.byte_size for item in self.files) > MAX_REPOSITORY_BYTES:
            msg = "manifest files exceed the service repository byte limit"
            raise ValueError(msg)
        return self


@dataclass(frozen=True)
class ServiceRepositorySnapshot:
    """The exact service-folder bytes under a service repository and their manifest."""

    manifest: ServiceRepositoryManifest
    files: dict[str, bytes]


def _unreadable(error: OSError) -> None:
    msg = f"{error.filename}: service repository is unreadable"
    raise InputError(msg) from error


def _check_entry(path: Path) -> None:
    if path.is_symlink():
        msg = f"{path}: service repository contains a symbolic link"
        raise InputError(msg)
    if path.name.startswith("."):
        msg = f"{path}: service repository may not contain dotfiles"
        raise InputError(msg)
    if not re.fullmatch(NAME_PATTERN, path.name):
        msg = (
            f"{path}: names may hold only letters, digits, '.', '_' and '-' "
            "and must start with a letter or digit"
        )
        raise InputError(msg)
    if not path.is_dir() and not stat.S_ISREG(path.lstat().st_mode):
        msg = f"{path}: service repository holds a file that is not regular"
        raise InputError(msg)


def _read_within_limits(root: Path, path: Path, files: Mapping[str, bytes]) -> bytes:
    if len(files) >= MAX_REPOSITORY_FILES:
        msg = f"{root}: service repository exceeds {MAX_REPOSITORY_FILES} files"
        raise InputError(msg)
    captured = sum(map(len, files.values()))
    # The size is checked before reading so a stray large file is never loaded.
    if captured + path.lstat().st_size <= MAX_REPOSITORY_BYTES:
        content = path.read_bytes()
        if captured + len(content) <= MAX_REPOSITORY_BYTES:
            return content
    msg = f"{root}: service repository exceeds {MAX_REPOSITORY_BYTES} bytes"
    raise InputError(msg)


def capture_service_repository(root: Path) -> ServiceRepositorySnapshot:
    """Capture every regular file in the service folders under a directory."""
    if not root.is_dir() or root.is_symlink():
        msg = f"{root}: service repository is not a directory"
        raise InputError(msg)
    files: dict[str, bytes] = {}
    for directory, subdirectories, filenames in os.walk(root, onerror=_unreadable):
        current = Path(directory)
        # Sorted, so the first reported error never depends on the filesystem.
        subdirectories.sort()
        filenames.sort()
        # Files at the repository root, such as its README, belong to no service.
        names = subdirectories if current == root else subdirectories + filenames
        for name in names:
            _check_entry(current / name)
        if current == root:
            continue
        if current.parent == root and DESCRIPTION_FILENAME not in filenames:
            msg = f"{current}: service folder has no {DESCRIPTION_FILENAME}"
            raise InputError(msg)
        for name in filenames:
            path = current / name
            files[path.relative_to(root).as_posix()] = _read_within_limits(
                root, path, files
            )
    if not files:
        msg = f"{root}: no service folders found"
        raise InputError(msg)
    files = dict(sorted(files.items()))
    try:
        manifest = ServiceRepositoryManifest.model_validate(
            {
                "schema_version": "sdi.service-repository-manifest/v1",
                "files": [
                    {
                        "path": path,
                        "byte_size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                    for path, content in files.items()
                ],
            }
        )
    except ValidationError as error:
        msg = f"{root}: service repository manifest is invalid: {error}"
        raise InputError(msg) from error
    return ServiceRepositorySnapshot(manifest, files)


def parse_service_repository(files: Mapping[str, bytes]) -> list[DescriptionFile]:
    """Validate the captured descriptions on their own and against each other."""
    parsed = [
        _parse(path, content)
        for path, content in sorted(files.items())
        if PurePosixPath(path).parts[1:] == (DESCRIPTION_FILENAME,)
    ]
    _check_cross_file(parsed)
    return parsed


def read_service_repository(root: Path) -> list[DescriptionFile]:
    """Capture a supplied directory and validate each service folder's SDI.md."""
    return parse_service_repository(capture_service_repository(root).files)
