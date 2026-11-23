"""Python-owned operations used by the thin Jenkins scheduler."""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ._bundle import assemble_bundle, validate_bundle
from ._git_input import GitRepository
from ._handoff_contracts import HANDOFF_CONTRACT_VERSION
from ._integration_code import integration_code_digest, require_fresh_agent_images
from ._jenkins_agent_boundary import enforce_integration_execution_boundary
from ._result_contracts import run_is_red
from ._run_input import identify_committed_run
from ._service_descriptions import SERVICE_REPOSITORY_ROOT
from ._stage_registry import (
    INTEGRATION_AGENT,
    RUN_LIMITS,
    STAGE_ORDER,
    STAGES,
    SUPPLIED_INPUT_SLOTS,
)
from ._stage_runtime import (
    StageExecution,
    check_key_bindings,
    committed_stage_sources,
    composition_key_names,
    execute_identified_stage,
    load_stage_adapter,
    load_stage_execution,
    reject_ungranted_supplied_inputs,
    skipped_stage_execution,
    stage_inputs,
)
from ._yaml_input import InputError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from ._stage_registry import StageName
    from ._stage_runtime import StageInputSource, SuppliedInputs

POSITIVE_DECIMAL = re.compile(r"^[1-9][0-9]{0,19}$")
STAGE_WIRING_BEGIN = "// BEGIN GENERATED STAGE WIRING"
STAGE_WIRING_END = "// END GENERATED STAGE WIRING"
WRITE_STAGE_WIRING_COMMAND = (
    "uv run --project integration sdi-integration jenkinsfile --write"
    " --jenkinsfile Jenkinsfile"
)


class RunDeadlineExpiredError(Exception):
    """Signal that Jenkins must finalize without launching another Stage."""


def _groovy_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def render_stage_wiring() -> str:
    lines = [
        STAGE_WIRING_BEGIN,
        "// Rendered from _stage_registry.py. After editing the registry, run this",
        f"// from the repository root: {WRITE_STAGE_WIRING_COMMAND}",
        f"def integrationLabel = {_groovy_string(INTEGRATION_AGENT.label)}",
        f"def runLimitSeconds = {RUN_LIMITS.run_seconds}",
        "def stages = [",
    ]
    for stage in STAGES.values():
        credentials = ", ".join(
            f"[id: {_groovy_string(credential.jenkins_id)},"
            f" variable: {_groovy_string(credential.variable)}]"
            for credential in stage.credentials
        )
        safety_timeout_seconds = (
            stage.grant.work_limit_seconds + RUN_LIMITS.stage_safety_margin_seconds
        )
        lines += [
            (
                f"    [name: {_groovy_string(stage.display_name)},"
                f" stage: {_groovy_string(stage.name)},"
                f" label: {_groovy_string(stage.agent.label)},"
            ),
            f"     safetyTimeoutSeconds: {safety_timeout_seconds},",
            f"     credentials: [{credentials}]],",
        ]
    lines += ["]", STAGE_WIRING_END]
    return "".join(f"{line}\n" for line in lines)


def _split_stage_wiring(jenkinsfile: str) -> tuple[str, str, str]:
    begin = jenkinsfile.find(f"{STAGE_WIRING_BEGIN}\n")
    end = jenkinsfile.find(f"{STAGE_WIRING_END}\n")
    if not 0 <= begin < end or any(
        jenkinsfile.count(marker) != 1
        for marker in (STAGE_WIRING_BEGIN, STAGE_WIRING_END)
    ):
        msg = (
            f"Jenkinsfile needs exactly one {STAGE_WIRING_BEGIN!r} line followed by"
            f" one {STAGE_WIRING_END!r} line"
        )
        raise InputError(msg)
    end += len(STAGE_WIRING_END) + 1
    return jenkinsfile[:begin], jenkinsfile[begin:end], jenkinsfile[end:]


def write_stage_wiring(jenkinsfile_path: Path) -> None:
    before, _block, after = _split_stage_wiring(
        jenkinsfile_path.read_text(encoding="utf-8")
    )
    jenkinsfile_path.write_text(
        before + render_stage_wiring() + after, encoding="utf-8"
    )


def check_stage_wiring(jenkinsfile_path: Path) -> None:
    _before, block, _after = _split_stage_wiring(
        jenkinsfile_path.read_text(encoding="utf-8")
    )
    if block != render_stage_wiring():
        msg = (
            "Jenkinsfile Stage wiring is stale; run from the repository root:"
            f" {WRITE_STAGE_WIRING_COMMAND}"
        )
        raise InputError(msg)


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
) -> tuple[dict[str, Any], GitRepository, dict[str, StageInputSource]]:
    identified = identify_committed_run(
        repository_path=repository_path,
        requested_ref=requested_ref,
        resolved_commit=resolved_commit,
        run_request_path=run_request_path,
        execution_id=execution_id,
    )
    repository = GitRepository(repository_path, resolved_commit)
    repository.require_ref_commit(requested_ref)
    return identified, repository, committed_stage_sources(repository, identified)


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
    baked_code_digests: Mapping[str, str],
    supplied: SuppliedInputs,
) -> dict[str, Any]:
    """Revalidate one submitted run, every Stage, and allocated resources.

    Every agent image must bake the submitted commit's integration code; that
    is checked before anything else the agents' code judges.
    """
    enforce_integration_execution_boundary()
    if dict(resource_nodes) != {"integration": INTEGRATION_AGENT.node} | {
        name: stage.agent.node for name, stage in STAGES.items()
    }:
        msg = "Jenkins resource mapping does not match the reviewed topology"
        raise InputError(msg)
    require_fresh_agent_images(
        integration_code_digest(repository_path / "integration"),
        {
            resource_nodes[resource]: digest
            for resource, digest in baked_code_digests.items()
        },
        integration_node=resource_nodes["integration"],
    )
    _validate_handoff_scalars(
        handoff_contract_version=handoff_contract_version,
        github_run_id=github_run_id,
        github_run_attempt=github_run_attempt,
    )
    parse_build_started_at(build_started_at_millis)
    identified, repository, _committed = _identify_submitted_run(
        repository_path=repository_path,
        requested_ref=requested_ref,
        resolved_commit=resolved_commit,
        run_request_path=run_request_path,
        execution_id=execution_id,
    )
    key_names = composition_key_names(repository, supplied)
    for expected_stage, stage in STAGES.items():
        descriptor, _ = load_stage_adapter(repository, stage.jenkins_descriptor)
        if descriptor.stage != expected_stage:
            msg = f"{expected_stage} does not match its reviewed Jenkins Stage"
            raise InputError(msg)
        check_key_bindings(descriptor, key_names)
    return identified


def _load_execution_prefix(
    roots: Sequence[Path],
    execution_id: str,
    initial_inputs: Mapping[str, StageInputSource],
) -> tuple[list[StageExecution], dict[str, StageInputSource]]:
    """Load accepted attempts and their outputs, comparing recomputed inputs.

    A supplied input this process was not granted is history: the Stage that
    accepted it compared it, and it is not captured again here.
    """
    if len(roots) > len(STAGE_ORDER):
        msg = "accepted attempt transfer contains too many Stages"
        raise InputError(msg)
    executions: list[StageExecution] = []
    available_inputs = dict(initial_inputs)
    outputs: dict[str, StageInputSource] = {}
    for index, root in enumerate(roots):
        expected_stage = STAGE_ORDER[index]
        if executions and executions[-1].blocks_downstream:
            msg = "accepted attempt transfer contains work after a blocker"
            raise InputError(msg)
        execution = load_stage_execution(
            root,
            expected_execution_id=execution_id,
            expected_stage=expected_stage,
        )
        for accepted in execution.envelope.accepted_inputs:
            if (
                accepted.slot in SUPPLIED_INPUT_SLOTS
                and accepted.slot not in initial_inputs
            ):
                continue
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
        if not execution.blocks_downstream:
            available_inputs.update(execution.outputs)
            outputs.update(execution.outputs)
    return executions, outputs


def execute_jenkins_stage(  # noqa: PLR0913
    *,
    repository_path: Path,
    requested_ref: str,
    resolved_commit: str,
    run_request_path: str,
    execution_id: str,
    stage: StageName,
    attempt_root: Path,
    prior_attempt_roots: Sequence[Path],
    work_limit_seconds: int | None,
    run_deadline_epoch_millis: str,
    baked_code_digest: str,
    supplied: SuppliedInputs,
) -> dict[str, Any]:
    """Execute the next Stage from validated committed and stashed inputs.

    The executing agent's image must bake the submitted commit's integration
    code; that is checked first, so stale code judges nothing else.
    """
    definition = STAGES[stage]
    require_fresh_agent_images(
        integration_code_digest(repository_path / "integration"),
        {definition.agent.node: baked_code_digest},
    )
    minimum_seconds = definition.grant.minimum_work_limit_seconds
    granted_seconds = definition.grant.work_limit_seconds
    if work_limit_seconds is None:
        work_limit_seconds = granted_seconds
    elif not minimum_seconds <= work_limit_seconds <= granted_seconds:
        msg = (
            f"--work-limit-seconds must be between {minimum_seconds} and the"
            f" {stage} Stage's granted {granted_seconds} seconds"
        )
        raise InputError(msg)
    identified, repository, committed = _identify_submitted_run(
        repository_path=repository_path,
        requested_ref=requested_ref,
        resolved_commit=resolved_commit,
        run_request_path=run_request_path,
        execution_id=execution_id,
    )
    reject_ungranted_supplied_inputs(supplied, stage=stage)
    if "service_repository" in definition.grant.inputs:
        supplied = replace(
            supplied, service_repository=repository_path / SERVICE_REPOSITORY_ROOT
        )
    granted = stage_inputs(repository, identified, supplied, stage=stage)
    executions, outputs = _load_execution_prefix(
        prior_attempt_roots,
        execution_id,
        committed | granted,
    )
    if executions and executions[-1].blocks_downstream:
        msg = "a blocking accepted attempt prevents further Stage execution"
        raise InputError(msg)
    next_stage_index = len(executions)
    if next_stage_index >= len(STAGE_ORDER):
        msg = "all reviewed Stages already have accepted attempts"
        raise InputError(msg)
    expected = STAGE_ORDER[next_stage_index]
    if stage != expected:
        msg = f"next accepted attempt must execute the {expected} Stage"
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
        descriptor_path=definition.jenkins_descriptor,
        attempt_root=attempt_root,
        available_inputs=granted | outputs,
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
    return not execution.blocks_downstream


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
    identified, repository, committed = _identify_submitted_run(
        repository_path=repository_path,
        requested_ref=requested_ref,
        resolved_commit=resolved_commit,
        run_request_path=run_request_path,
        execution_id=execution_id,
    )
    executions, _outputs = _load_execution_prefix(
        attempt_roots,
        execution_id,
        committed,
    )
    blocker = next((item for item in executions if item.blocks_downstream), None)
    if len(executions) < len(STAGE_ORDER) and blocker is None:
        pending = STAGES[STAGE_ORDER[len(executions)]]
        if not run_deadline_expired:
            msg = f"Jenkins finalization is missing the {pending.name} accepted attempt"
            raise InputError(msg)
        descriptor, _profile = load_stage_adapter(
            repository, pending.jenkins_descriptor
        )
        blocker = skipped_stage_execution(
            execution_id=execution_id,
            stage=pending.name,
            implementation_mode=descriptor.implementation_mode,
            code="sdi.run.deadline-exceeded",
            summary="The Pipeline integration run deadline expired before Stage work.",
        )
        executions.append(blocker)

    for stage in STAGE_ORDER[len(executions) :]:
        if blocker is None:
            msg = "Jenkins finalization has no blocking attempt"
            raise InputError(msg)
        descriptor, _profile = load_stage_adapter(
            repository, STAGES[stage].jenkins_descriptor
        )
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


def bundle_is_red(bundle_root: Path, *, execution_id: str) -> bool:
    result = validate_bundle(bundle_root)
    if result.execution_id != execution_id:
        msg = "Pipeline integration result execution ID does not match the build"
        raise InputError(msg)
    return run_is_red(result)
