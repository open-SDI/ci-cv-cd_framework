"""Deterministic generation and freshness checks for public JSON Schemas."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

from ._contracts import (
    MobilityRequirementsSpecification,
    RunRequest,
    TargetExecutionProfile,
)
from ._domain_contracts import (
    CompositionBlueprint,
    DeploymentResult,
    DeploymentSchema,
    ImageBuildResult,
    ValidationEvidence,
)
from ._handoff_contracts import HandoffReceipt
from ._recovery_contracts import RecoveryManifest
from ._result_contracts import PipelineIntegrationResult
from ._stage_contracts import (
    AcceptedAttemptEnvelope,
    AdapterDescriptor,
    AdapterRequest,
    AdapterResponse,
    FixtureCase,
    StageProfile,
)
from ._yaml_input import InputError

SCHEMAS: dict[str, type[BaseModel]] = {
    "accepted-attempt-envelope-v1.schema.json": AcceptedAttemptEnvelope,
    "adapter-descriptor-v1.schema.json": AdapterDescriptor,
    "composition-blueprint-v1.schema.json": CompositionBlueprint,
    "deployment-schema-v1.schema.json": DeploymentSchema,
    "deployment-result-v1.schema.json": DeploymentResult,
    "fixture-case-v1.schema.json": FixtureCase,
    "handoff-receipt-v1.schema.json": HandoffReceipt,
    "image-build-result-v1.schema.json": ImageBuildResult,
    "pipeline-integration-result-v1.schema.json": PipelineIntegrationResult,
    "pipeline-integration-run-request-v1.schema.json": RunRequest,
    "recovery-manifest-v1.schema.json": RecoveryManifest,
    "stage-adapter-request-v1.schema.json": AdapterRequest,
    "stage-adapter-response-v1.schema.json": AdapterResponse,
    "stage-profile-v1.schema.json": StageProfile,
    "mobility-requirements-specification-v1.schema.json": (
        MobilityRequirementsSpecification
    ),
    "target-execution-profile-v1.schema.json": TargetExecutionProfile,
    "validation-evidence-v1.schema.json": ValidationEvidence,
}


def _remove_null_defaults(value: object) -> None:
    if isinstance(value, dict):
        mapping = cast("dict[object, object]", value)
        if mapping.get("default", object()) is None:
            del mapping["default"]
        for nested in mapping.values():
            _remove_null_defaults(nested)
    elif isinstance(value, list):
        for nested in cast("list[object]", value):
            _remove_null_defaults(nested)


def _schema_bytes(model: type[BaseModel]) -> bytes:
    schema = model.model_json_schema(mode="validation")
    _remove_null_defaults(schema)
    text = json.dumps(
        schema,
        allow_nan=False,
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    )
    return f"{text}\n".encode()


def write_schemas(directory: Path) -> None:
    """Write every authoritative contract schema deterministically."""
    directory.mkdir(parents=True, exist_ok=True)
    for filename, model in SCHEMAS.items():
        (directory / filename).write_bytes(_schema_bytes(model))


def check_schemas(directory: Path) -> None:
    """Reject missing, stale, or obsolete generated contract schemas."""
    actual_names = {path.name for path in directory.glob("*.schema.json")}
    expected_names = set(SCHEMAS)
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        obsolete = sorted(actual_names - expected_names)
        msg = f"schema set is stale; missing={missing}, obsolete={obsolete}"
        raise InputError(msg)
    stale = [
        filename
        for filename, model in SCHEMAS.items()
        if (directory / filename).read_bytes() != _schema_bytes(model)
    ]
    if stale:
        msg = f"generated schemas are stale: {sorted(stale)}"
        raise InputError(msg)
