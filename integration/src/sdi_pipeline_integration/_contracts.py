"""Authoritative v1 input contracts for pipeline integration runs."""

from __future__ import annotations

from typing import Annotated, Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    StringConstraints,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema  # noqa: TC002

RUN_REQUEST_SCHEMA_VERSION = "sdi.pipeline-integration-run-request/v1"
REQUIREMENTS_SCHEMA_VERSION = "sdi.mobility-requirements-specification/v1"
TARGET_PROFILE_SCHEMA_VERSION = "sdi.target-execution-profile/v1"
REPOSITORY_PATH_SCHEMA_PATTERN = (
    r"^(?!/)(?![A-Za-z]:/)(?!.*(?:^|/)\.{1,2}(?:/|$))(?!.*//)(?!.*\\)"
    r"(?!.*[\x00-\x1f\x7f]).*[^/]$"
)
GITHUB_REPOSITORY_IDENTITY_PATTERN = r"^github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Slug = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$",
        max_length=63,
    ),
]
ScenarioId = Annotated[str, StringConstraints(strict=True, pattern=r"^S-[0-9]{2}$")]
TestcaseId = Annotated[str, StringConstraints(strict=True, pattern=r"^TC-[0-9]{2}$")]
CombinationId = Annotated[str, StringConstraints(strict=True, pattern=r"^C-[0-9]{2}$")]
RepositoryPath = Annotated[
    str,
    StringConstraints(strict=True, min_length=1),
    Field(json_schema_extra={"pattern": REPOSITORY_PATH_SCHEMA_PATTERN}),
]
RepositoryIdentity = Annotated[
    str,
    StringConstraints(strict=True, pattern=GITHUB_REPOSITORY_IDENTITY_PATTERN),
]


class ContractModel(BaseModel):
    """Base for closed, immutable, strict input contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
    )


def reject_explicit_nulls(data: object, field_names: tuple[str, ...]) -> object:
    if isinstance(data, dict):
        values = cast("dict[object, object]", data)
        for field_name in field_names:
            if field_name in values and values[field_name] is None:
                msg = f"{field_name} must be omitted when unknown"
                raise ValueError(msg)
    return cast("object", data)


class RunCombination(ContractModel):
    """The one testcase-local combination selected by a run request."""

    combination_id: CombinationId
    target_profile: RepositoryPath


class RunRequest(ContractModel):
    """One committed pipeline integration run request."""

    schema_version: Literal["sdi.pipeline-integration-run-request/v1"]
    scenario_id: ScenarioId
    testcase_id: TestcaseId
    requirements_specification: RepositoryPath
    combination: RunCombination


def _require_unique(values: list[str], field_name: str) -> None:
    if len(values) != len(set(values)):
        msg = f"{field_name} values must be unique"
        raise ValueError(msg)


class MobilityRequirementsSpecification(ContractModel):
    """Front matter of a Markdown Mobility Requirements Specification."""

    schema_version: Literal["sdi.mobility-requirements-specification/v1"]
    document_version: PositiveInt
    requirement_ids: Annotated[list[Slug], Field(min_length=1)]

    @model_validator(mode="after")
    def require_unique_requirement_ids(self) -> Self:
        """Give each requirement one stable identity."""
        _require_unique(self.requirement_ids, "requirement_id")
        return self


class MobilityTarget(ContractModel):
    """The mobility target described by a reusable profile."""

    target_id: Slug
    kind: Literal["sdv", "sdr"]


class Baseline(ContractModel):
    """Operating system and ROS baseline of a host or required by an artifact."""

    os: NonBlank
    ros_distro: NonBlank
    jetpack: NonBlank | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require an unknown JetPack version to be omitted rather than null."""
        return reject_explicit_nulls(data, ("jetpack",))


class Endpoint(ContractModel):
    """One ROS interface keyed by its exact name in a role map."""

    type: NonBlank
    reliability: Literal["reliable", "best_effort"] | SkipJsonSchema[None] = None
    durability: Literal["volatile", "transient_local"] | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require undeclared QoS to be omitted rather than null."""
        return reject_explicit_nulls(data, ("reliability", "durability"))


type RoleMap = dict[NonBlank, Endpoint]
ROLE_MAPS = (
    "subscribes",
    "publishes",
    "service_servers",
    "service_clients",
    "action_servers",
    "action_clients",
)


class RoleMaps(ContractModel):
    """The six ROS role maps; an omitted map is unknown."""

    subscribes: RoleMap | SkipJsonSchema[None] = None
    publishes: RoleMap | SkipJsonSchema[None] = None
    service_servers: RoleMap | SkipJsonSchema[None] = None
    service_clients: RoleMap | SkipJsonSchema[None] = None
    action_servers: RoleMap | SkipJsonSchema[None] = None
    action_clients: RoleMap | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknown_role_maps(cls, data: object) -> object:
        """Require unknown role maps to be omitted rather than null."""
        return reject_explicit_nulls(data, ROLE_MAPS)


class Host(ContractModel):
    """One execution host; a declared device list is complete."""

    role: Literal["native", "controller", "edge"]
    architecture: Literal["arm64", "amd64"]
    baseline: Baseline | SkipJsonSchema[None] = None
    devices: list[NonBlank] | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require unknown host facts to be omitted rather than null."""
        return reject_explicit_nulls(data, ("baseline", "devices"))


Connection = Annotated[list[Slug], Field(min_length=2, max_length=2)]


class TargetExecutionProfile(ContractModel):
    """A reusable description of one candidate execution environment."""

    schema_version: Literal["sdi.target-execution-profile/v1"]
    profile_id: Slug
    target: MobilityTarget
    hosts: Annotated[dict[Slug, Host], Field(min_length=1)]
    connections: list[Connection] | SkipJsonSchema[None] = None
    orchestrator_provides: RoleMaps | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require absent optional sections to be omitted rather than null."""
        return reject_explicit_nulls(data, ("connections", "orchestrator_provides"))

    @model_validator(mode="after")
    def validate_connections(self) -> Self:
        """Require unordered connections between two distinct profile hosts."""
        pairs: list[frozenset[str]] = []
        for connection in self.connections or []:
            pair = frozenset(connection)
            if len(pair) != len(connection) or pair - set(self.hosts):
                msg = "connection must name two distinct profile hosts"
                raise ValueError(msg)
            pairs.append(pair)
        if len(pairs) != len(set(pairs)):
            msg = "connections must be unique"
            raise ValueError(msg)
        return self
