"""Implemented composition Stage adapter: generate, assess and publish.

The adapter discovers the service descriptions relevant to the requirements and
profile, asks the selected generator for proposals over those, assesses them
against the whole repository, and publishes the preferred proposal as the
composition blueprint and deployment schema. Every successful execution also
publishes evidence of the discovery trace and the exact request, response and
assessment. Reason
summaries and the diagnostic come only from fixed templates, so provider text
never reaches durable output.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, NamedTuple

from ._adapter_kit import Outcome, Verdict, serve
from ._assessment import AssessmentInputs, assess_proposals
from ._composition_config import CompositionSettings
from ._contracts import (
    MobilityRequirementsSpecification,
    RunRequest,
    TargetExecutionProfile,
)
from ._discovery import EMBEDDINGS_TIMEOUT_SECONDS, discover
from ._domain_contracts import (
    CompositionBlueprint,
    CompositionEvidence,
    DeploymentSchema,
)
from ._generation import generate_proposals
from ._json_input import parse_json
from ._service_descriptions import (
    SERVICE_REPOSITORY_ROOT,
    ServiceRepositoryManifest,
    capture_service_repository,
    parse_service_repository,
)
from ._stage_registry import SLOT_BY_NAME
from ._yaml_input import (
    InputError,
    parse_front_matter,
    parse_yaml,
    validate_contract,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

    from ._adapter_kit import Invocation
    from ._assessment import AssessedProposal
    from ._service_descriptions import ServiceDescription

# Leave the runtime time to capture the candidate before its work limit expires.
TIMEOUT_RESERVE_SECONDS = 30

OUTCOMES = {
    outcome.code: outcome
    for outcome in (
        Outcome(
            "preferred-proposal",
            "succeeded",
            "succeeded",
            "domain",
            "The assessment preferred one eligible generated proposal.",
        ),
        Outcome(
            "bounded-no-result",
            "succeeded",
            "failed",
            "domain",
            "Generation reached its output limit before returning proposals.",
        ),
        Outcome(
            "scoped-rejection",
            "succeeded",
            "failed",
            "domain",
            "Every proposal lost all of its services to placement removals.",
        ),
        Outcome(
            "insufficient-proposals",
            "succeeded",
            "failed",
            "domain",
            "No generated proposal was eligible.",
        ),
        Outcome(
            "discovery-no-result",
            "failed",
            "not_evaluated",
            "input",
            "Discovery retrieved no service description.",
        ),
        Outcome(
            "discovery-over-budget",
            "failed",
            "not_evaluated",
            "input",
            "No retrieved service description fits the context budget.",
        ),
        Outcome(
            "dense-discovery-unavailable",
            "failed",
            "not_evaluated",
            "tool",
            "The declared embeddings endpoint was unavailable to discovery.",
        ),
        Outcome(
            "invalid-service-description",
            "failed",
            "not_evaluated",
            "input",
            "A supplied service description is invalid.",
        ),
        Outcome(
            "generation-invalid",
            "failed",
            "not_evaluated",
            "tool",
            "The model returned output outside the proposal contract.",
        ),
        Outcome(
            "generation-refused",
            "failed",
            "not_evaluated",
            "tool",
            "The model refused to generate proposals.",
        ),
        Outcome(
            "provider-error",
            "failed",
            "not_evaluated",
            "tool",
            "The model provider did not complete the request.",
        ),
    )
}
ASSESSMENT_OUTCOMES = {
    "preferred": "preferred-proposal",
    "scoped-rejection": "scoped-rejection",
    "insufficient": "insufficient-proposals",
}
GENERATION_FAILURES = {
    "invalid": "generation-invalid",
    "refused": "generation-refused",
    "provider_error": "provider-error",
}


def _blueprint_documents(
    *,
    preferred: AssessedProposal,
    services: dict[str, ServiceDescription],
    identity: dict[str, object],
    requirement_ids: list[str],
) -> dict[str, BaseModel]:
    """Turn the preferred proposal's checked outcome into the Domain outputs."""
    kept = preferred.kept
    kept_ids = {item.service_id for item in kept}
    coverage = {entry.requirement_id: entry for entry in preferred.coverage or []}
    blueprint_id = (
        f"{identity['scenario_id']}-{identity['testcase_id']}-"
        f"{identity['combination_id']}-{preferred.proposal_id}"
    ).lower()
    blueprint = validate_contract(
        CompositionBlueprint,
        identity
        | {
            "schema_version": SLOT_BY_NAME["composition_blueprint"].schema_version,
            "blueprint_id": blueprint_id,
            "proposal_id": preferred.proposal_id,
            "requirement_ids": requirement_ids,
            "services": [
                {
                    "service_id": item.service_id,
                    "basis": services[item.service_id].provenance.basis,
                    "capability": item.capability,
                    "depends_on": services[item.service_id].depends_on or [],
                }
                for item in kept
            ],
            "coverage": [
                {
                    "requirement_id": requirement_id,
                    "status": coverage[requirement_id].status,
                    "services": [
                        service_id
                        for service_id in coverage[requirement_id].services
                        if service_id in kept_ids
                    ],
                }
                for requirement_id in requirement_ids
            ],
        },
        SLOT_BY_NAME["composition_blueprint"].file_name,
    )
    deployment = validate_contract(
        DeploymentSchema,
        identity
        | {
            "schema_version": SLOT_BY_NAME["deployment_schema"].schema_version,
            "deployment_schema_id": f"{blueprint_id}-deployment",
            "blueprint_id": blueprint_id,
            # Only a placement with both an artifact and a host is resolved.
            "placements": [
                {"service_id": item.service_id, "status": item.placement}
                | (
                    {}
                    if item.artifact_id is None
                    else {"artifact_id": item.artifact_id}
                )
                | ({} if item.host is None else {"host": item.host})
                for item in kept
            ],
        },
        SLOT_BY_NAME["deployment_schema"].file_name,
    )
    return {"composition_blueprint": blueprint, "deployment_schema": deployment}


class Composed(NamedTuple):
    code: str
    documents: dict[str, BaseModel]
    lexical_only: bool


def _composition_settings(
    content: bytes, path: str, work_limit_seconds: int
) -> CompositionSettings:
    """Return the run's settings, with a generation timeout that fits the limit."""
    settings = validate_contract(
        CompositionSettings, parse_json(content, path, max_bytes=len(content)), path
    )
    reserve = TIMEOUT_RESERVE_SECONDS + (
        0 if settings.discovery.embeddings is None else EMBEDDINGS_TIMEOUT_SECONDS
    )
    config = settings.generator_config
    timeout = max(1, min(config.timeout_seconds, work_limit_seconds - reserve))
    return settings.model_copy(
        update={
            "generator_config": config.model_copy(update={"timeout_seconds": timeout})
        }
    )


def _compose(invocation: Invocation) -> Composed:
    request = invocation.request
    diagnostic = invocation.diagnostic
    contents = invocation.inputs
    paths = {item.slot: item.path for item in request.inputs}
    manifest = validate_contract(
        ServiceRepositoryManifest,
        parse_json(
            contents["service_repository"],
            paths["service_repository"],
            max_bytes=len(contents["service_repository"]),
        ),
        paths["service_repository"],
    )
    snapshot = capture_service_repository(
        invocation.input_root / SERVICE_REPOSITORY_ROOT
    )
    if snapshot.manifest != manifest:
        msg = "copied service folders do not match the service-repository manifest"
        raise InputError(msg)
    run_request = validate_contract(
        RunRequest,
        parse_yaml(contents["run_request"], paths["run_request"]),
        paths["run_request"],
    )
    profile = validate_contract(
        TargetExecutionProfile,
        parse_yaml(contents["target_profile"], paths["target_profile"]),
        paths["target_profile"],
    )
    front_matter, requirements_body = parse_front_matter(
        contents["requirements_specification"], paths["requirements_specification"]
    )
    requirements = validate_contract(
        MobilityRequirementsSpecification,
        front_matter,
        paths["requirements_specification"],
    )
    composition = _composition_settings(
        contents["composition_settings"],
        paths["composition_settings"],
        request.work_limit_seconds,
    )
    config = composition.generator_config
    embeddings = composition.discovery.embeddings
    settings = composition.evidence_settings()
    # A URL never matches the diagnostic fact pattern; the evidence records it.
    diagnostic.facts({name: v for name, v in settings.items() if name != "base_url"})
    identity: dict[str, object] = {
        "evidence_basis": "implemented",
        "scenario_id": run_request.scenario_id,
        "testcase_id": run_request.testcase_id,
        "combination_id": run_request.combination.combination_id,
        "profile_id": profile.profile_id,
        "profile_basis": profile.provenance.basis,
        "source_inputs": [
            {"slot": item.slot, "sha256": item.sha256} for item in request.inputs
        ],
    }

    try:
        files = parse_service_repository(snapshot.files)
    except InputError as error:
        # Per-file errors name their description first; cross-file ones may not.
        path = str(error).split(":", 1)[0]
        if path in snapshot.files:
            diagnostic.line(f"invalid_description: {path}")
        return Composed("invalid-service-description", {}, lexical_only=False)
    inputs = AssessmentInputs(files, profile, requirements, requirements_body)

    discovery = discover(
        inputs,
        embeddings=embeddings,
        max_context_bytes=config.max_context_bytes,
    )
    record = discovery.record
    diagnostic.fact("discovery_selected", len(discovery.selected))
    diagnostic.fact("discovery_omitted", len(discovery.omitted))
    diagnostic.fact("dense_status", discovery.dense_status)
    stopped, lexical_only = discovery.stop_reason(embeddings)
    if stopped is not None:
        return Composed(stopped, {}, lexical_only)
    generation = generate_proposals(inputs=inputs, discovery=discovery, config=config)
    diagnostic.facts(generation.provider_facts())
    if generation.outcome not in {"generated", "exhausted"}:
        return Composed(GENERATION_FAILURES[generation.outcome], {}, lexical_only)
    evidence: dict[str, object] = identity | {
        "schema_version": "sdi.composition-evidence/v1",
        "descriptions": [{"path": item.path, "sha256": item.sha256} for item in files],
        "generation": {
            **settings,
            "outcome": generation.outcome,
            "request": generation.request,
            "response": generation.response,
        },
        "discovery": record,
    }
    proposals = generation.proposals
    if proposals is None:
        return Composed(
            "bounded-no-result",
            {
                "composition_evidence": validate_contract(
                    CompositionEvidence, evidence, "composition-evidence.json"
                )
            },
            lexical_only,
        )
    assessment = assess_proposals(proposals, inputs)
    assessed = assessment.to_evidence()
    if assessment.preferred is None and not discovery.complete:
        # Search limits bound this result; they say nothing about feasibility.
        assessed["reason"] = (
            f"{assessed['reason']} within the bounded supplied context; "
            "see discovery.omitted"
        )
    documents: dict[str, BaseModel] = {
        "composition_evidence": validate_contract(
            CompositionEvidence,
            evidence | {"assessment": assessed},
            "composition-evidence.json",
        )
    }
    if assessment.preferred is None:
        return Composed(
            ASSESSMENT_OUTCOMES[assessment.outcome], documents, lexical_only
        )
    diagnostic.fact("preferred", assessment.preferred.proposal_id)
    return Composed(
        "preferred-proposal",
        _blueprint_documents(
            preferred=assessment.preferred,
            services={file.description.service_id: file.description for file in files},
            identity=identity,
            requirement_ids=requirements.requirement_ids,
        )
        | documents,
        lexical_only,
    )


def _composition(invocation: Invocation) -> Verdict:
    code, documents, lexical_only = _compose(invocation)
    outcome = OUTCOMES[code]
    if outcome.conclusion == "failed":
        invocation.diagnostic.line(
            "omitted: composition evidence, which failed attempts do not publish"
        )
    if lexical_only:
        outcome = replace(
            outcome,
            summary=f"{outcome.summary} Embeddings were unavailable, so "
            "discovery was lexical only.",
        )
    return Verdict(outcome, documents)


main = serve("composition", _composition)
