"""Python-owned operations used by the thin Jenkins scheduler."""

from __future__ import annotations

import hashlib
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ._git_input import GitRepository
from ._handoff_contracts import HANDOFF_CONTRACT_VERSION
from ._jenkins_agent_boundary import enforce_integration_execution_boundary
from ._local_dispatch import DESCRIPTORS, assemble_bundle, validate_bundle
from ._run_input import identify_committed_run
from ._stage_runtime import (
    StageExecution,
    committed_stage_sources,
    execute_identified_stage,
    load_stage_adapter,
    load_stage_execution,
    skipped_stage_execution,
)
from ._yaml_input import InputError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from ._result_contracts import PipelineIntegrationResult
    from ._stage_contracts import StageName
    from ._stage_runtime import StageInputSource

EXPECTED_STAGE_CONFIGURATION = {
    "composition": ("composition", 10 * 60),
    "image_build": ("image-build", 30 * 60),
    "cv": ("cv", 45 * 60),
    "cd": ("cd", 15 * 60),
}
EXPECTED_RESOURCE_NODES = {
    "integration": "integration",
    "composition": "ci",
    "image_build": "image-build",
    "cv": "cv",
    "cd": "cd",
}
POSITIVE_DECIMAL = re.compile(r"^[1-9][0-9]{0,19}$")
MAX_WORK_LIMIT_SECONDS = 24 * 60 * 60


class RunDeadlineExpiredError(Exception):
    """Signal that Jenkins must finalize without launching another Stage."""


def _validate_handoff_scalars(
    *,
    handoff_contract_version: str,
    github_run_id: str,
    github_run_attempt: str,
) -> None:
    if handoff_contract_version != HANDOFF_CONTRACT_VERSION:
        msg = "unsupported GitHub-to-Jenkins handoff contract version"
        raise InputError(msg)
    if POSITIVE_DECIMAL.fullmatch(github_run_id) is None:
        msg = "GitHub run ID must be a positive decimal integer"
        raise InputError(msg)
    if POSITIVE_DECIMAL.fullmatch(github_run_attempt) is None:
        msg = "GitHub run attempt must be a positive decimal integer"
        raise InputError(msg)


def parse_build_started_at(build_started_at_millis: str) -> datetime:
    """Parse the Jenkins-owned build start used by the result timeline."""
    if POSITIVE_DECIMAL.fullmatch(build_started_at_millis) is None:
        msg = "Jenkins build start must be positive epoch milliseconds"
        raise InputError(msg)
    try:
        started_at = datetime.fromtimestamp(
            int(build_started_at_millis) / 1000,
            tz=UTC,
        )
    except (OverflowError, OSError, ValueError) as error:
        msg = "Jenkins build start is outside the supported timestamp range"
        raise InputError(msg) from error
    if started_at > datetime.now(UTC):
        msg = "Jenkins build start cannot be in the future"
        raise InputError(msg)
    return started_at


def _identify_submitted_run(
    *,
    repository_path: Path,
    requested_ref: str,
    resolved_commit: str,
    run_request_path: str,
    execution_id: str,
) -> tuple[dict[str, Any], GitRepository]:
    identified = identify_committed_run(
        repository_path=repository_path,
        requested_ref=requested_ref,
        resolved_commit=resolved_commit,
        run_request_path=run_request_path,
        execution_id=execution_id,
    )
    repository = GitRepository(repository_path, resolved_commit)
    repository.require_ref_commit(requested_ref)
    return identified, repository


def preflight_jenkins_run(  # noqa: PLR0913
    *,
    repository_path: Path,
    requested_ref: str,
    resolved_commit: str,
    run_request_path: str,
    execution_id: str,
    handoff_contract_version: str,
    github_run_id: str,
    github_run_attempt: str,
    build_started_at_millis: str,
    resource_nodes: Mapping[str, str],
) -> dict[str, Any]:
    """Revalidate one submitted run, every Stage, and allocated resources."""
    enforce_integration_execution_boundary()
    _validate_handoff_scalars(
        handoff_contract_version=handoff_contract_version,
        github_run_id=github_run_id,
        github_run_attempt=github_run_attempt,
    )
    parse_build_started_at(build_started_at_millis)
    if dict(resource_nodes) != EXPECTED_RESOURCE_NODES:
        msg = "Jenkins resource mapping does not match the reviewed topology"
        raise InputError(msg)
    identified, repository = _identify_submitted_run(
        repository_path=repository_path,
        requested_ref=requested_ref,
        resolved_commit=resolved_commit,
        run_request_path=run_request_path,
        execution_id=execution_id,
    )
    for expected_stage, descriptor_path in DESCRIPTORS:
        descriptor, profile = load_stage_adapter(repository, descriptor_path)
        expected_label, expected_limit = EXPECTED_STAGE_CONFIGURATION[expected_stage]
        if (
            descriptor.stage != expected_stage
            or descriptor.agent_label != expected_label
            or profile.work_limit_seconds != expected_limit
        ):
            msg = f"{expected_stage} does not match its reviewed Jenkins Stage"
            raise InputError(msg)
    committed_stage_sources(repository, identified)
    return identified


def _blocks_downstream(execution: StageExecution) -> bool:
    envelope = execution.envelope
    return (
        envelope.lifecycle_state == "skipped"
        or envelope.execution_conclusion != "succeeded"
        or envelope.domain_outcome == "failed"
        or envelope.implementation_mode == "not_implemented"
    )


def _load_execution_prefix(
    roots: Sequence[Path],
    execution_id: str,
    initial_inputs: Mapping[str, StageInputSource],
) -> tuple[list[StageExecution], dict[str, StageInputSource]]:
    if len(roots) > len(DESCRIPTORS):
        msg = "accepted attempt transfer contains too many Stages"
        raise InputError(msg)
    executions: list[StageExecution] = []
    available_inputs = dict(initial_inputs)
    for index, root in enumerate(roots):
        expected_stage = DESCRIPTORS[index][0]
        if executions and _blocks_downstream(executions[-1]):
            msg = "accepted attempt transfer contains work after a blocker"
            raise InputError(msg)
        execution = load_stage_execution(
            root,
            expected_execution_id=execution_id,
            expected_stage=expected_stage,
        )
        for accepted in execution.envelope.accepted_inputs:
            source = available_inputs.get(accepted.slot)
            if source is None or (
                accepted.source_path,
                accepted.schema_version,
                accepted.byte_size,
                accepted.sha256,
            ) != (
                source.source_path,
                source.schema_version,
                len(source.content),
                hashlib.sha256(source.content).hexdigest(),
            ):
                msg = "accepted attempt input provenance does not match its source"
                raise InputError(msg)
        executions.append(execution)
        if not _blocks_downstream(execution):
            available_inputs.update(execution.outputs)
    return executions, available_inputs


def execute_jenkins_stage(  # noqa: PLR0913
    *,
    repository_path: Path,
    requested_ref: str,
    resolved_commit: str,
    run_request_path: str,
    execution_id: str,
    descriptor_path: str,
    attempt_root: Path,
    prior_attempt_roots: Sequence[Path],
    work_limit_seconds: int,
    run_deadline_epoch_millis: str,
) -> dict[str, Any]:
    """Execute the next Stage from validated committed and stashed inputs."""
    identified, repository = _identify_submitted_run(
        repository_path=repository_path,
        requested_ref=requested_ref,
        resolved_commit=resolved_commit,
        run_request_path=run_request_path,
        execution_id=execution_id,
    )
    committed_inputs = committed_stage_sources(repository, identified)
    executions, available_inputs = _load_execution_prefix(
        prior_attempt_roots,
        execution_id,
        committed_inputs,
    )
    if executions and _blocks_downstream(executions[-1]):
        msg = "a blocking accepted attempt prevents further Stage execution"
        raise InputError(msg)
    next_stage_index = len(executions)
    if next_stage_index >= len(DESCRIPTORS):
        msg = "all reviewed Stages already have accepted attempts"
        raise InputError(msg)
    expected_stage, expected_descriptor = DESCRIPTORS[next_stage_index]
    if descriptor_path != expected_descriptor:
        msg = f"next accepted attempt must execute the {expected_stage} descriptor"
        raise InputError(msg)
    if work_limit_seconds <= 0 or work_limit_seconds > MAX_WORK_LIMIT_SECONDS:
        msg = "Stage work limit must be between 1 and 86400 seconds"
        raise InputError(msg)

    if POSITIVE_DECIMAL.fullmatch(run_deadline_epoch_millis) is None:
        msg = "run deadline must be positive epoch milliseconds"
        raise InputError(msg)
    remaining_seconds = max(
        0.0,
        int(run_deadline_epoch_millis) / 1000 - time.time(),
    )
    if remaining_seconds == 0:
        raise RunDeadlineExpiredError
    execution = execute_identified_stage(
        repository=repository,
        identified=identified,
        descriptor_path=descriptor_path,
        attempt_root=attempt_root,
        available_inputs=available_inputs,
        work_limit_seconds=work_limit_seconds,
        deadline_monotonic=time.monotonic() + remaining_seconds,
    )
    return execution.envelope.model_dump(mode="json", exclude_none=True)


def attempt_allows_continuation(
    attempt_root: Path,
    *,
    execution_id: str,
    stage: StageName,
) -> bool:
    """Derive whether Jenkins may schedule the next Domain Stage."""
    execution = load_stage_execution(
        attempt_root,
        expected_execution_id=execution_id,
        expected_stage=stage,
    )
    return not _blocks_downstream(execution)


def finalize_jenkins_run(  # noqa: PLR0913
    *,
    repository_path: Path,
    requested_ref: str,
    resolved_commit: str,
    run_request_path: str,
    execution_id: str,
    build_started_at_millis: str,
    attempt_roots: Sequence[Path],
    run_deadline_expired: bool,
    bundle_root: Path,
) -> dict[str, Any]:
    """Materialize skips and assemble one complete validated Jenkins result."""
    enforce_integration_execution_boundary()
    identified, repository = _identify_submitted_run(
        repository_path=repository_path,
        requested_ref=requested_ref,
        resolved_commit=resolved_commit,
        run_request_path=run_request_path,
        execution_id=execution_id,
    )
    committed_inputs = committed_stage_sources(repository, identified)
    executions, _available_inputs = _load_execution_prefix(
        attempt_roots,
        execution_id,
        committed_inputs,
    )
    blocker = next((item for item in executions if _blocks_downstream(item)), None)
    if len(executions) < len(DESCRIPTORS) and blocker is None:
        if not run_deadline_expired:
            stage = DESCRIPTORS[len(executions)][0]
            msg = f"Jenkins finalization is missing the {stage} accepted attempt"
            raise InputError(msg)
        stage, descriptor_path = DESCRIPTORS[len(executions)]
        descriptor, _profile = load_stage_adapter(repository, descriptor_path)
        blocker = skipped_stage_execution(
            execution_id=execution_id,
            stage=stage,
            implementation_mode=descriptor.implementation_mode,
            code="sdi.run.deadline-exceeded",
            summary="The Pipeline integration run deadline expired before Stage work.",
        )
        executions.append(blocker)

    for stage, descriptor_path in DESCRIPTORS[len(executions) :]:
        if blocker is None:
            msg = "Jenkins finalization has no blocking attempt"
            raise InputError(msg)
        descriptor, _profile = load_stage_adapter(repository, descriptor_path)
        blocked_stage = blocker.envelope.correlation.stage
        executions.append(
            skipped_stage_execution(
                execution_id=execution_id,
                stage=stage,
                implementation_mode=descriptor.implementation_mode,
                code="sdi.dependency.prerequisite-blocked",
                summary=(
                    f"The {blocked_stage} Stage outcome blocked this dependent Stage."
                ),
            )
        )
    return assemble_bundle(
        identified=identified,
        run_started_at=parse_build_started_at(build_started_at_millis),
        executions=executions,
        bundle_root=bundle_root,
    )


def bundle_has_machinery_failure(bundle_root: Path, *, execution_id: str) -> bool:
    """Validate a bundle and derive only its machinery conclusion."""
    result: PipelineIntegrationResult = validate_bundle(bundle_root)
    if result.execution_id != execution_id:
        msg = "Pipeline integration result execution ID does not match the build"
        raise InputError(msg)
    for attempt in result.attempts:
        if attempt.execution_conclusion in {"failed", "timed_out"}:
            return True
        if attempt.reason is not None and attempt.reason.code in {
            "sdi.stage.not-implemented",
            "sdi.dependency.fixture-evidence",
            "sdi.run.deadline-exceeded",
        }:
            return True
    return False
