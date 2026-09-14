"""Deep module for validating and identifying one committed run."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from ._contracts import (
    REQUIREMENTS_SCHEMA_VERSION,
    RUN_REQUEST_SCHEMA_VERSION,
    TARGET_PROFILE_SCHEMA_VERSION,
    MobilityRequirementsSpecification,
    RunRequest,
    TargetExecutionProfile,
)
from ._git_input import CommittedBlob, GitRepository, validate_repository_path
from ._yaml_input import InputError, parse_yaml

if TYPE_CHECKING:
    from pathlib import Path

PROTECTED_MAIN_REF = "refs/heads/main"
INPUT_FILE_COUNT = 3
UUID_VERSION = 4


@dataclass(frozen=True)
class InputProvenance:
    """Exact-byte provenance for one accepted committed input."""

    path: str
    schema_version: str
    byte_size: int
    sha256: str


def _validate_model[ModelT: BaseModel](
    model: type[ModelT], blob: CommittedBlob
) -> ModelT:
    parsed = parse_yaml(blob.content, blob.path)
    try:
        return model.model_validate(parsed, strict=True, extra="forbid")
    except ValidationError as error:
        msg = f"{blob.path}: contract validation failed: {error}"
        raise InputError(msg) from error


def _provenance(blob: CommittedBlob, schema_version: str) -> InputProvenance:
    return InputProvenance(
        path=blob.path,
        schema_version=schema_version,
        byte_size=len(blob.content),
        sha256=hashlib.sha256(blob.content).hexdigest(),
    )


def identify_committed_run(
    *,
    repository_path: Path,
    requested_ref: str,
    resolved_commit: str,
    run_request_path: str,
    execution_id: str | None = None,
) -> dict[str, Any]:
    """Validate one committed request and accept or assign its identity."""
    if requested_ref != PROTECTED_MAIN_REF:
        msg = f"requested ref must be {PROTECTED_MAIN_REF}"
        raise InputError(msg)
    validate_repository_path(run_request_path)
    repository = GitRepository(repository_path, resolved_commit)
    repository.require_ref_commit(requested_ref)

    request_blob = repository.read_regular_file(run_request_path)
    request = _validate_model(RunRequest, request_blob)
    referenced_paths = [
        request.requirements_specification,
        request.combination.target_profile,
    ]
    if len({run_request_path, *referenced_paths}) != INPUT_FILE_COUNT:
        msg = "request and referenced input paths must be distinct"
        raise InputError(msg)

    requirements_blob = repository.read_regular_file(request.requirements_specification)
    _validate_model(MobilityRequirementsSpecification, requirements_blob)
    profile_blob = repository.read_regular_file(request.combination.target_profile)
    profile = _validate_model(TargetExecutionProfile, profile_blob)

    inputs = [
        _provenance(request_blob, RUN_REQUEST_SCHEMA_VERSION),
        _provenance(requirements_blob, REQUIREMENTS_SCHEMA_VERSION),
        _provenance(profile_blob, TARGET_PROFILE_SCHEMA_VERSION),
    ]
    if execution_id is None:
        accepted_execution_id = str(uuid.uuid4())
    else:
        try:
            parsed_execution_id = uuid.UUID(execution_id)
        except (ValueError, AttributeError) as error:
            msg = "execution ID must be a lowercase UUIDv4"
            raise InputError(msg) from error
        if (
            parsed_execution_id.version != UUID_VERSION
            or str(parsed_execution_id) != execution_id
        ):
            msg = "execution ID must be a lowercase UUIDv4"
            raise InputError(msg)
        accepted_execution_id = execution_id

    return {
        "execution_id": accepted_execution_id,
        "repository": repository.identity,
        "requested_ref": requested_ref,
        "resolved_commit_sha": repository.commit_sha,
        "scenario_id": request.scenario_id,
        "testcase_id": request.testcase_id,
        "combination_id": request.combination.combination_id,
        "profile_id": profile.profile_id,
        "inputs": [asdict(item) for item in inputs],
    }
