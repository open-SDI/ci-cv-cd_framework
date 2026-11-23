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

import argparse
import hashlib
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from ._adapter_output import atomic_write
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
from ._jenkins_agent_boundary import enforce_domain_execution_boundary
from ._json_input import canonical_json, parse_json
from ._service_descriptions import (
    SERVICE_REPOSITORY_ROOT,
    ServiceRepositoryManifest,
    capture_service_repository,
    parse_service_repository,
)
from ._stage_contracts import (
    AdapterRequest,
    AdapterResponse,
    contains_sensitive_material,
)
from ._yaml_input import (
    InputError,
    parse_front_matter,
    parse_yaml,
    validate_contract,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ._assessment import AssessedProposal
    from ._service_descriptions import ServiceDescription

MAX_REQUEST_BYTES = 128 * 1024
# Leave the runtime time to capture the candidate before its work limit expires.
TIMEOUT_RESERVE_SECONDS = 30
# Model names may carry an organization prefix, as in Qwen/Qwen3.8-27B.
DIAGNOSTIC_FACT = re.compile(r"^[A-Za-z0-9_./-]{1,128}$")

OUTCOMES: dict[str, tuple[str, str, str, str]] = {
    "preferred-proposal": (
        "succeeded",
        "succeeded",
        "domain",
        "The assessment preferred one eligible generated proposal.",
    ),
    "bounded-no-result": (
        "succeeded",
        "failed",
        "domain",
        "Generation reached its output limit before returning proposals.",
    ),
    "scoped-rejection": (
        "succeeded",
        "failed",
        "domain",
        "Every proposal lost all of its services to placement removals.",
    ),
    "insufficient-proposals": (
        "succeeded",
        "failed",
        "domain",
        "No generated proposal was eligible.",
    ),
    "discovery-no-result": (
        "failed",
        "not_evaluated",
        "input",
        "Discovery retrieved no service description.",
    ),
    "discovery-over-budget": (
        "failed",
        "not_evaluated",
        "input",
        "No retrieved service description fits the context budget.",
    ),
    "dense-discovery-unavailable": (
        "failed",
        "not_evaluated",
        "tool",
        "The declared embeddings endpoint was unavailable to discovery.",
    ),
    "invalid-service-description": (
        "failed",
        "not_evaluated",
        "input",
        "A supplied service description is invalid.",
    ),
    "generation-invalid": (
        "failed",
        "not_evaluated",
        "tool",
        "The model returned output outside the proposal contract.",
    ),
    "generation-refused": (
        "failed",
        "not_evaluated",
        "tool",
        "The model refused to generate proposals.",
    ),
    "provider-error": (
        "failed",
        "not_evaluated",
        "tool",
        "The model provider did not complete the request.",
    ),
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


def _read_input(path: Path, size: int, sha256: str) -> bytes:
    if path.is_symlink() or not path.is_file() or path.lstat().st_nlink != 1:
        msg = "adapter input is not a single regular file"
        raise InputError(msg)
    content = path.read_bytes()
    if len(content) != size or hashlib.sha256(content).hexdigest() != sha256:
        msg = "adapter input bytes do not match the request"
        raise InputError(msg)
    return content


class _Run:
    """One adapter invocation and the candidate files it publishes."""

    def __init__(self, request: AdapterRequest, output_root: Path) -> None:
        self.request = request
        self.output_root = output_root
        self.outputs = {item.slot: item.path for item in request.outputs}
        self.facts: dict[str, object] = {}
        # Discovery fell back to lexical only, as the config allows.
        self.lexical_only = False
        # A manifest path, already checked against the manifest's path pattern.
        self.invalid_description: str | None = None

    def publish(self, code: str, documents: dict[str, BaseModel]) -> None:
        """Publish every candidate file, then the response."""
        conclusion, domain_outcome, category, summary = OUTCOMES[code]
        for slot, document in documents.items():
            atomic_write(
                self.output_root,
                self.outputs[slot],
                canonical_json(document.model_dump(mode="json", exclude_none=True)),
            )
        atomic_write(
            self.output_root, self.request.diagnostic.path, self._diagnostic(code)
        )
        response = AdapterResponse.model_validate(
            {
                "schema_version": "sdi.stage-adapter-response/v1",
                "correlation": self.request.correlation.model_dump(mode="json"),
                "execution_conclusion": conclusion,
                "domain_outcome": domain_outcome,
                "reason": {
                    "category": category,
                    "code": f"sdi.composition.{code}",
                    "summary": f"{summary} Embeddings were unavailable, so "
                    "discovery was lexical only."
                    if self.lexical_only
                    else summary,
                },
                "consumed_inputs": [item.slot for item in self.request.inputs],
                "produced_outputs": list(documents),
                "diagnostic": {"present": True, "truncated": False},
            },
            strict=True,
            extra="forbid",
        )
        atomic_write(
            self.output_root,
            "response.json",
            canonical_json(response.model_dump(mode="json")),
        )

    def _diagnostic(self, code: str) -> bytes:
        """Fill the fixed diagnostic template with checked run facts only."""
        lines = [f"outcome: sdi.composition.{code}"]
        for name, value in self.facts.items():
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                continue
            text = str(value)
            if DIAGNOSTIC_FACT.fullmatch(text) is None or contains_sensitive_material(
                text
            ):
                text = "unrecorded"
            lines.append(f"{name}: {text}")
        if self.invalid_description is not None:
            lines.append(f"invalid_description: {self.invalid_description}")
        if OUTCOMES[code][0] == "failed":
            lines.append(
                "omitted: composition evidence, which failed attempts do not publish"
            )
        content = "\n".join(lines) + "\n"
        return content.encode()[: self.request.diagnostic.max_bytes]


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
            "schema_version": "sdi.composition-blueprint/v1",
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
        "composition-blueprint.json",
    )
    deployment = validate_contract(
        DeploymentSchema,
        identity
        | {
            "schema_version": "sdi.deployment-schema/v1",
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
        "deployment-schema.json",
    )
    return {"composition_blueprint": blueprint, "deployment_schema": deployment}


type Outcome = tuple[str, dict[str, BaseModel]]


def _composition_settings(
    content: bytes, work_limit_seconds: int
) -> CompositionSettings:
    """Return the run's settings, with a generation timeout that fits the limit."""
    settings = validate_contract(
        CompositionSettings,
        parse_json(content, "composition-settings.json", max_bytes=len(content)),
        "composition-settings.json",
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


def _compose(run: _Run, input_root: Path) -> Outcome:
    """Return the outcome code and the documents the run publishes."""
    request = run.request
    declared = {item.slot: item for item in request.inputs}
    contents = {
        slot: _read_input(input_root / item.path, item.byte_size, item.sha256)
        for slot, item in declared.items()
    }
    manifest = validate_contract(
        ServiceRepositoryManifest,
        parse_json(
            contents["service_repository"],
            "service-repository.json",
            max_bytes=len(contents["service_repository"]),
        ),
        "service-repository.json",
    )
    snapshot = capture_service_repository(input_root / SERVICE_REPOSITORY_ROOT)
    if snapshot.manifest != manifest:
        msg = "copied descriptions do not match the service-repository manifest"
        raise InputError(msg)
    run_request = validate_contract(
        RunRequest,
        parse_yaml(contents["run_request"], "run-request.yaml"),
        "run-request.yaml",
    )
    profile = validate_contract(
        TargetExecutionProfile,
        parse_yaml(contents["target_profile"], "target-profile.yaml"),
        "target-profile.yaml",
    )
    front_matter, requirements_body = parse_front_matter(
        contents["requirements_specification"], "requirements-specification.md"
    )
    requirements = validate_contract(
        MobilityRequirementsSpecification, front_matter, "requirements-specification.md"
    )
    composition = _composition_settings(
        contents["composition_settings"], request.work_limit_seconds
    )
    config = composition.generator_config
    embeddings = composition.discovery.embeddings
    settings = composition.evidence_settings()
    # A URL never matches the diagnostic fact pattern; the evidence records it.
    run.facts |= {name: value for name, value in settings.items() if name != "base_url"}
    identity: dict[str, object] = {
        "evidence_basis": "implemented",
        "scenario_id": run_request.scenario_id,
        "testcase_id": run_request.testcase_id,
        "combination_id": run_request.combination.combination_id,
        "profile_id": profile.profile_id,
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
            run.invalid_description = path
        return "invalid-service-description", {}
    inputs = AssessmentInputs(files, profile, requirements, requirements_body)

    discovery = discover(
        inputs,
        embeddings=embeddings,
        max_context_bytes=config.max_context_bytes,
    )
    record = discovery.record
    run.facts |= {
        "discovery_selected": len(discovery.selected),
        "discovery_omitted": len(discovery.omitted),
        "dense_status": discovery.dense_status,
    }
    run.facts["discovery_over_budget"] = discovery.over_budget
    stopped, run.lexical_only = discovery.stop_reason(embeddings)
    if stopped is not None:
        return stopped, {}
    generation = generate_proposals(inputs=inputs, discovery=discovery, config=config)
    run.facts |= generation.provider_facts()
    if generation.outcome not in {"generated", "exhausted"}:
        return GENERATION_FAILURES[generation.outcome], {}
    evidence: dict[str, object] = identity | {
        "schema_version": "sdi.composition-evidence/v1",
        "descriptions": [
            {"path": item.path, "sha256": item.sha256} for item in manifest.files
        ],
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
        return "bounded-no-result", {
            "composition_evidence": validate_contract(
                CompositionEvidence, evidence, "composition-evidence.json"
            )
        }
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
        return ASSESSMENT_OUTCOMES[assessment.outcome], documents
    run.facts["preferred"] = assessment.preferred.proposal_id
    return "preferred-proposal", _blueprint_documents(
        preferred=assessment.preferred,
        services={file.description.service_id: file.description for file in files},
        identity=identity,
        requirement_ids=requirements.requirement_ids,
    ) | documents


def run_adapter(*, request_path: Path, input_root: Path, output_root: Path) -> None:
    """Publish one complete candidate response after all candidate files."""
    enforce_domain_execution_boundary(expected_label="composition")
    try:
        parsed = parse_json(
            request_path.read_bytes(), str(request_path), max_bytes=MAX_REQUEST_BYTES
        )
    except OSError as error:
        msg = "adapter request cannot be read"
        raise InputError(msg) from error
    request = validate_contract(AdapterRequest, parsed, str(request_path))
    if request.correlation.stage != "composition":
        msg = "the composition adapter serves only the composition Stage"
        raise InputError(msg)
    if (
        not input_root.is_dir()
        or not output_root.is_dir()
        or any(output_root.iterdir())
    ):
        msg = "adapter roots must exist and the output root must be empty"
        raise InputError(msg)
    run = _Run(request, output_root)
    run.publish(*_compose(run, input_root))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdi-composition-adapter")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--request", type=Path, required=True)
    run.add_argument("--input-root", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Expose only the stable one-shot Stage-adapter operation."""
    try:
        enforce_domain_execution_boundary()
        arguments = _parser().parse_args(argv)
        run_adapter(
            request_path=arguments.request,
            input_root=arguments.input_root,
            output_root=arguments.output_root,
        )
    except (InputError, OSError, ValidationError) as error:
        sys.stderr.write(f"sdi-composition-adapter: {type(error).__name__}\n")
        return 2
    return 0
