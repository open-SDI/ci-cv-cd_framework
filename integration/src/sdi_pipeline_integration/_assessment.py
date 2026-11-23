"""Checking and ranking of model-generated composition proposals."""

from __future__ import annotations

import functools
import re
from collections import Counter
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Annotated, Literal, cast

from pydantic import Field

from ._contracts import (
    Baseline,
    ContractModel,
    Endpoint,
    Host,
    MobilityRequirementsSpecification,
    NonBlank,
    RoleMaps,
    TargetExecutionProfile,
)
from ._domain_contracts import Capability  # noqa: TC001
from ._json_input import parse_json
from ._service_descriptions import AptRoute, ImageRoute, read_service_repository
from ._yaml_input import parse_front_matter, parse_yaml, validate_contract

if TYPE_CHECKING:
    from pathlib import Path

    from ._service_descriptions import (
        Artifact,
        DescriptionFile,
        Route,
        ServiceDescription,
    )

MAX_PROPOSALS_BYTES = 1024 * 1024
ROLE_PAIRS = (
    ("subscribes", "publishes"),
    ("service_clients", "service_servers"),
    ("action_clients", "action_servers"),
)
GAP_KINDS = frozenset(
    {
        "missing-provider",
        "missing-artifact",
        "unmet-dependency",
        "unresolved-placement",
        "unresolved-connection",
    }
)
# A digest or a full commit names fixed content; anything else may move.
IMAGE_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
FULL_COMMIT = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")

type CoverageStatus = Literal[
    "supported", "missing", "uncertain", "outside_composition"
]
type PlacementStatus = Literal["resolved", "unresolved", "rejected"]
type AssessmentOutcome = Literal["preferred", "scoped-rejection", "insufficient"]
type RankingCriterion = Literal["supported", "gaps", "model-order"]
RANKING = {
    "criteria": (
        "most requirements supported after the checks",
        "then fewest gaps",
    ),
    "performance_evidence": "none available, so it decides nothing",
    "ties": "first in the model's order",
}
NO_PREFERENCE_REASONS: dict[AssessmentOutcome, str] = {
    "scoped-rejection": "every proposal lost all of its services to placement removals",
    "insufficient": "no proposal is eligible",
}


class ServicePlacement(ContractModel):
    """One chosen service, its artifact and its profile host."""

    service_id: NonBlank
    artifact_id: NonBlank | None = None
    host: NonBlank | None = None
    capability: Capability


class CoverageClaim(ContractModel):
    """The model's claim for one requirement obligation."""

    requirement_id: NonBlank
    status: CoverageStatus
    services: list[NonBlank]
    reason: NonBlank


class Proposal(ContractModel):
    """One joint services, artifacts and placement proposal."""

    rationale: NonBlank
    services: Annotated[list[ServicePlacement], Field(min_length=1)]
    coverage: list[CoverageClaim]


class CompositionProposals(ContractModel):
    """The model's alternatives, listed best-first."""

    proposals: Annotated[list[Proposal], Field(min_length=1, max_length=3)]


@dataclass(frozen=True)
class _Participant:
    """A chosen service, or the orchestrator when service_id is None."""

    service_id: str | None
    host: str | None
    roles: RoleMaps

    @property
    def label(self) -> str:
        return self.service_id or "orchestrator"


@dataclass(frozen=True)
class PlacedService:
    """One proposed service after its artifact and host checks."""

    service_id: str
    capability: str
    artifact_id: str | None
    route: Route | None
    host: str | None
    placement: PlacementStatus


@dataclass(frozen=True)
class Score:
    """Counts that rank an eligible proposal; only supported and gaps decide."""

    supported: int
    gaps: int
    missing: int
    uncertain: int

    @property
    def key(self) -> tuple[int, int]:
        """Sort key: most supported first, then fewest gaps."""
        return (-self.supported, self.gaps)


@dataclass(frozen=True)
class AssessedProposal:
    """One proposal and the outcome of every check it reached.

    Services are absent when the proposal failed the checks that precede
    placement; the score is absent whenever the proposal is ineligible.
    """

    proposal_id: str
    proposal: Proposal
    findings: list[dict[str, object]]
    services: list[PlacedService] | None = None
    bindings: list[dict[str, object]] | None = None
    coverage: list[CoverageClaim] | None = None
    score: Score | None = None
    # How the ranking ordered this proposal behind the preferred one.
    decided_by: RankingCriterion | None = None

    @property
    def kept(self) -> list[PlacedService]:
        """The services that survive placement."""
        return [item for item in self.services or [] if item.placement != "rejected"]

    def to_evidence(self) -> dict[str, object]:
        """Return the JSON record of this proposal's assessment."""
        return {
            "proposal_id": self.proposal_id,
            "eligible": self.score is not None,
            "findings": self.findings,
            "services": None
            if self.services is None
            else [
                {
                    "service_id": item.service_id,
                    "artifact_id": item.artifact_id,
                    "route": None
                    if item.route is None
                    else item.route.model_dump(mode="json"),
                    "host": item.host,
                    "placement": item.placement,
                }
                for item in self.services
            ],
            "bindings": self.bindings,
            "coverage": None
            if self.coverage is None
            else [entry.model_dump(mode="json") for entry in self.coverage],
            "score": None
            if self.score is None
            else {
                "supported": self.score.supported,
                "gaps": self.score.gaps,
                "missing": self.score.missing,
                "uncertain": self.score.uncertain,
            },
            "decided_by": self.decided_by,
            "proposal": self.proposal.model_dump(mode="json", exclude_unset=True),
        }


@dataclass(frozen=True)
class Assessment:
    """Every assessed proposal and the one preferred, if any is eligible."""

    proposals: list[AssessedProposal]
    preferred: AssessedProposal | None
    tie_broken_by_model_order: bool
    order: list[str]

    @property
    def outcome(self) -> AssessmentOutcome:
        """Say whether a proposal is preferred and, if not, why none is."""
        if self.preferred is not None:
            return "preferred"
        # A proposal that passed the checks preceding placement can only become
        # ineligible by losing every service there.
        if all(item.services is not None for item in self.proposals):
            return "scoped-rejection"
        return "insufficient"

    def to_evidence(self) -> dict[str, object]:
        """Return the JSON record of the whole assessment."""
        return {
            "preferred": None if self.preferred is None else self.preferred.proposal_id,
            "tie_broken_by_model_order": self.tie_broken_by_model_order,
            "reason": NO_PREFERENCE_REASONS.get(self.outcome),
            "ranking": RANKING
            | {"criteria": list(RANKING["criteria"]), "order": self.order},
            "proposals": [item.to_evidence() for item in self.proposals],
        }


def _finding(
    kind: str,
    message: str,
    *,
    service_id: str | None = None,
    requirement_id: str | None = None,
) -> dict[str, object]:
    return {
        "kind": kind,
        "gap": kind in GAP_KINDS,
        "message": message,
        "service_id": service_id,
        "requirement_id": requirement_id,
    }


def _ineligibility(
    proposal: Proposal,
    services: dict[str, ServiceDescription],
    requirement_ids: list[str],
) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for placement in proposal.services:
        service_id = placement.service_id
        description = services.get(service_id)
        if description is None:
            findings.append(
                _finding("ineligible", "unknown service_id", service_id=service_id)
            )
        elif (
            placement.artifact_id is not None
            and description.artifacts is not None
            and placement.artifact_id not in description.artifacts
        ):
            findings.append(
                _finding(
                    "ineligible",
                    f"unknown artifact_id {placement.artifact_id}",
                    service_id=service_id,
                )
            )
    counts = Counter(placement.service_id for placement in proposal.services)
    findings.extend(
        _finding("ineligible", "duplicate service_id", service_id=service_id)
        for service_id, count in counts.items()
        if count > 1
    )
    claimed = Counter(entry.requirement_id for entry in proposal.coverage)
    if claimed != Counter(requirement_ids):
        findings.append(
            _finding(
                "ineligible", "coverage does not name each requirement_id exactly once"
            )
        )
    findings.extend(
        _finding(
            "ineligible",
            f"{entry.status} coverage cites services",
            requirement_id=entry.requirement_id,
        )
        for entry in proposal.coverage
        if entry.status in {"missing", "outside_composition"} and entry.services
    )
    return findings


def _baseline_conflict(required: Baseline, actual: Baseline) -> str | None:
    for field in ("os", "ros_distro", "jetpack"):
        wanted, offered = getattr(required, field), getattr(actual, field)
        if wanted is not None and offered is not None and wanted != offered:
            return f"requires {field} {wanted}, host has {offered}"
    return None


def _artifact(
    placement: ServicePlacement, description: ServiceDescription
) -> tuple[Artifact | None, list[dict[str, object]]]:
    service_id = placement.service_id
    if description.artifacts is None:
        message = "declares no artifacts"
    elif placement.artifact_id is None:
        message = "no artifact chosen"
    else:
        return description.artifacts[placement.artifact_id], []
    return None, [_finding("missing-artifact", message, service_id=service_id)]


def _mutability(route: Route) -> str | None:
    """Say why a declared route may install different content later."""
    if isinstance(route, AptRoute):
        patterns = [package for package in route.packages if "*" in package]
        if patterns:
            return (
                f"apt pins {', '.join(patterns)} are version patterns resolved at "
                "install time; the apt repository may keep only its latest version"
            )
        return (
            "apt version pins are not content-addressed; the repository may drop them"
        )
    if isinstance(route, ImageRoute):
        if IMAGE_DIGEST.search(route.reference) is None:
            return f"image {route.reference} is not named by digest"
        return None
    if FULL_COMMIT.fullmatch(route.revision) is None:
        return f"source revision {route.revision} is not a full commit"
    return None


def _place(
    placement: ServicePlacement,
    description: ServiceDescription,
    hosts: dict[str, Host],
) -> tuple[PlacedService, list[dict[str, object]]]:
    """Return the placement outcome for one service and its findings."""
    service_id = placement.service_id
    artifact, findings = _artifact(placement, description)
    host = hosts.get(placement.host) if placement.host is not None else None
    placed = functools.partial(
        PlacedService,
        service_id=service_id,
        capability=placement.capability,
        artifact_id=placement.artifact_id if artifact is not None else None,
        route=artifact.route if artifact is not None else None,
        host=placement.host if host is not None else None,
    )
    if host is None:
        findings.append(
            _finding(
                "unresolved-placement",
                "no host chosen"
                if placement.host is None
                else f"host {placement.host} is not a profile host",
                service_id=service_id,
            )
        )
        return placed(placement="unresolved"), findings
    conflicts: list[str] = []
    if artifact is not None:
        if not {"noarch", host.architecture} & set(artifact.architectures):
            conflicts.append(f"no {host.architecture} artifact")
        if artifact.host_requirement is not None and host.baseline is not None:
            conflict = _baseline_conflict(artifact.host_requirement, host.baseline)
            if conflict is not None:
                conflicts.append(conflict)
    if description.devices:
        if host.devices is None:
            findings.append(
                _finding(
                    "unresolved-placement",
                    f"host {placement.host} declares no device list",
                    service_id=service_id,
                )
            )
        else:
            conflicts.extend(
                f"host lacks device {device}"
                for device in description.devices
                if device not in host.devices
            )
    if conflicts:
        rejection = _finding(
            "placement-rejected",
            f"on {placement.host}: {'; '.join(conflicts)}; service removed",
            service_id=service_id,
        )
        return placed(placement="rejected"), [rejection]
    return placed(placement="unresolved" if findings else "resolved"), findings


def _binding_conflict(
    consumer_role: str, wanted: Endpoint, offered: Endpoint
) -> str | None:
    if wanted.type != offered.type:
        return f"type {wanted.type} != {offered.type}"
    if consumer_role == "subscribes":
        if wanted.reliability == "reliable" and offered.reliability == "best_effort":
            return "reliable requested from a best_effort publisher"
        if wanted.durability == "transient_local" and offered.durability == "volatile":
            return "transient_local requested from a volatile publisher"
    return None


def _role(roles: RoleMaps, role: str) -> dict[str, Endpoint]:
    return cast("dict[str, Endpoint] | None", getattr(roles, role)) or {}


def _bind(
    participants: list[_Participant],
    connections: set[frozenset[str]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    bindings: list[dict[str, object]] = []
    findings: list[dict[str, object]] = []
    unconnected: dict[tuple[str, str], list[str]] = {}
    for consumer in participants:
        for consumer_role, provider_role in ROLE_PAIRS:
            for name, wanted in _role(consumer.roles, consumer_role).items():
                provided = False
                for provider in participants:
                    offered = _role(provider.roles, provider_role).get(name)
                    if provider is consumer or offered is None:
                        continue
                    edge = f"{name} {provider.label} -> {consumer.label}"
                    conflict = _binding_conflict(consumer_role, wanted, offered)
                    if conflict is not None:
                        findings.append(
                            _finding(
                                "binding-rejected",
                                f"{edge}: {conflict}",
                                service_id=consumer.service_id,
                            )
                        )
                        continue
                    provided = True
                    bindings.append(
                        {
                            "name": name,
                            "type": offered.type,
                            "consumer": {
                                "service_id": consumer.service_id,
                                "role": consumer_role,
                            },
                            "provider": {
                                "service_id": provider.service_id,
                                "role": provider_role,
                            },
                        }
                    )
                    if (
                        consumer.host is not None
                        and provider.host is not None
                        and consumer.host != provider.host
                        and frozenset((consumer.host, provider.host)) not in connections
                    ):
                        pair = (
                            min(consumer.host, provider.host),
                            max(consumer.host, provider.host),
                        )
                        unconnected.setdefault(pair, []).append(edge)
                if not provided:
                    findings.append(
                        _finding(
                            "missing-provider",
                            f"no provider for {consumer_role} {name}",
                            service_id=consumer.service_id,
                        )
                    )
    findings.extend(
        _finding(
            "unresolved-connection",
            f"no connection between {first} and {second}: {', '.join(edges)}",
        )
        for (first, second), edges in sorted(unconnected.items())
    )
    return bindings, findings


def _final_coverage(
    proposal: Proposal, present: set[str]
) -> tuple[list[CoverageClaim], list[dict[str, object]]]:
    coverage: list[CoverageClaim] = []
    findings: list[dict[str, object]] = []
    for entry in proposal.coverage:
        cited = set(entry.services)
        final = entry
        if entry.status == "supported" and not (cited and cited <= present):
            final = entry.model_copy(
                update={
                    "status": "missing",
                    "reason": "a cited service is not in the proposal",
                }
            )
        elif entry.status == "uncertain" and not cited & present:
            final = entry.model_copy(
                update={
                    "status": "missing",
                    "reason": "no cited service is in the proposal",
                }
            )
        if final is not entry:
            findings.append(
                _finding(
                    "coverage-downgraded",
                    f"{entry.status} -> missing: {final.reason}",
                    requirement_id=entry.requirement_id,
                )
            )
        coverage.append(final)
    return coverage, findings


def _check(
    proposal_id: str,
    proposal: Proposal,
    services: dict[str, ServiceDescription],
    profile: TargetExecutionProfile,
) -> AssessedProposal:
    findings: list[dict[str, object]] = []
    placed: list[PlacedService] = []
    participants: list[_Participant] = []
    dependencies: dict[str, list[str]] = {}
    for placement in proposal.services:
        description = services[placement.service_id]
        outcome, placement_findings = _place(placement, description, profile.hosts)
        placed.append(outcome)
        findings.extend(placement_findings)
        if outcome.placement == "rejected":
            continue
        participants.append(
            _Participant(placement.service_id, outcome.host, description)
        )
        dependencies[placement.service_id] = description.depends_on or []
        mutability = None if outcome.route is None else _mutability(outcome.route)
        if mutability is not None:
            findings.append(
                _finding(
                    "mutable-artifact", mutability, service_id=placement.service_id
                )
            )
        if not any(
            getattr(description, role) is not None
            for pair in ROLE_PAIRS
            for role in pair
        ):
            findings.append(
                _finding(
                    "endpoints-unverified",
                    "declares no role maps",
                    service_id=placement.service_id,
                )
            )
    if not participants:
        findings.append(_finding("ineligible", "no service remains after removals"))
        return AssessedProposal(proposal_id, proposal, findings, services=placed)
    findings.extend(
        _finding(
            "unmet-dependency",
            f"depends_on {dependency} is not in the proposal",
            service_id=service_id,
        )
        for service_id, needed in dependencies.items()
        for dependency in needed
        if dependency not in dependencies
    )
    orchestrator = _Participant(None, None, profile.orchestrator_provides or RoleMaps())
    bindings, bound = _bind(
        [*participants, orchestrator],
        {frozenset(pair) for pair in profile.connections or []},
    )
    findings.extend(bound)
    coverage, downgraded = _final_coverage(proposal, set(dependencies))
    findings.extend(downgraded)
    return AssessedProposal(
        proposal_id,
        proposal,
        findings,
        services=placed,
        bindings=bindings,
        coverage=coverage,
        score=Score(
            supported=sum(1 for entry in coverage if entry.status == "supported"),
            gaps=sum(1 for finding in findings if finding["gap"]),
            missing=sum(1 for entry in coverage if entry.status == "missing"),
            uncertain=sum(1 for entry in coverage if entry.status == "uncertain"),
        ),
    )


def assess_proposals(
    proposals: CompositionProposals, inputs: AssessmentInputs
) -> Assessment:
    """Check every proposal and prefer the eligible one supporting the most.

    Supported coverage ranks first and fewer gaps second; no performance
    evidence is available, so equal proposals keep the model's order.
    """
    services = {file.description.service_id: file.description for file in inputs.files}
    assessed: list[AssessedProposal] = []
    for index, proposal in enumerate(proposals.proposals):
        proposal_id = f"proposal-{index + 1}"
        ineligible = _ineligibility(
            proposal, services, inputs.requirements.requirement_ids
        )
        assessed.append(
            AssessedProposal(proposal_id, proposal, ineligible)
            if ineligible
            else _check(proposal_id, proposal, services, inputs.profile)
        )
    # Sorting is stable, so equal keys keep the model's order.
    ranked = sorted(
        (item for item in assessed if item.score is not None),
        key=lambda item: item.score.key if item.score else (0, 0),
    )
    if ranked:
        decided = {
            item.proposal_id: _decided_by(ranked[0], item) for item in ranked[1:]
        }
        assessed = [
            replace(item, decided_by=decided.get(item.proposal_id)) for item in assessed
        ]
    preferred = next(
        (
            item
            for item in assessed
            if ranked and item.proposal_id == ranked[0].proposal_id
        ),
        None,
    )
    return Assessment(
        proposals=assessed,
        preferred=preferred,
        tie_broken_by_model_order=len(ranked) > 1
        and _decided_by(ranked[0], ranked[1]) == "model-order",
        order=[item.proposal_id for item in ranked],
    )


def _decided_by(
    preferred: AssessedProposal, other: AssessedProposal
) -> RankingCriterion:
    """Name the first criterion that ranks other behind preferred."""
    first, second = cast("Score", preferred.score), cast("Score", other.score)
    if first.supported != second.supported:
        return "supported"
    if first.gaps != second.gaps:
        return "gaps"
    return "model-order"


@dataclass(frozen=True)
class AssessmentInputs:
    """The validated service repository, profile and requirements."""

    files: list[DescriptionFile]
    profile: TargetExecutionProfile
    requirements: MobilityRequirementsSpecification
    requirements_body: str


def load_assessment_inputs(
    *,
    service_repository: Path,
    target_profile: Path,
    requirements_specification: Path,
) -> AssessmentInputs:
    """Read and validate the inputs every proposal is generated and checked against."""
    files = read_service_repository(service_repository)
    profile = validate_contract(
        TargetExecutionProfile,
        parse_yaml(target_profile.read_bytes(), str(target_profile)),
        str(target_profile),
    )
    front_matter, body = parse_front_matter(
        requirements_specification.read_bytes(), str(requirements_specification)
    )
    requirements = validate_contract(
        MobilityRequirementsSpecification, front_matter, str(requirements_specification)
    )
    return AssessmentInputs(files, profile, requirements, body)


def assess_proposal_files(
    *,
    service_repository: Path,
    target_profile: Path,
    requirements_specification: Path,
    proposals: Path,
) -> Assessment:
    """Read the supplied inputs and assess the proposals they hold."""
    inputs = load_assessment_inputs(
        service_repository=service_repository,
        target_profile=target_profile,
        requirements_specification=requirements_specification,
    )
    document = parse_json(
        proposals.read_bytes(), str(proposals), max_bytes=MAX_PROPOSALS_BYTES
    )
    return assess_proposals(
        validate_contract(CompositionProposals, document, str(proposals)), inputs
    )
