"""Local four-Stage dispatch."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ._bundle import assemble_bundle_staging
from ._git_input import GitRepository
from ._run_input import identify_committed_run
from ._stage_registry import RUN_LIMITS, STAGES, StageName
from ._stage_runtime import (
    StageExecution,
    StageInputSource,
    SuppliedInputs,
    check_key_bindings,
    composition_key_names,
    execute_identified_stage,
    load_stage_adapter,
    skipped_stage_execution,
    stage_inputs,
)
from ._yaml_input import InputError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ._stage_contracts import ImplementationMode


def _descriptor_modes(
    repository: GitRepository, key_names: Sequence[str]
) -> dict[StageName, ImplementationMode]:
    modes: dict[StageName, ImplementationMode] = {}
    for expected_stage, stage in STAGES.items():
        descriptor, _profile = load_stage_adapter(repository, stage.local_descriptor)
        if descriptor.stage != expected_stage:
            msg = "reviewed descriptor order does not match its Stage"
            raise InputError(msg)
        check_key_bindings(descriptor, key_names)
        modes[expected_stage] = descriptor.implementation_mode
    return modes


def dispatch_local(  # noqa: PLR0913, PLR0915
    *,
    repository_path: Path,
    requested_ref: str,
    resolved_commit: str,
    run_request_path: str,
    bundle_root: Path,
    supplied: SuppliedInputs,
) -> dict[str, Any]:
    """Execute and atomically publish one complete local four-Stage run."""
    run_started_at = datetime.now(UTC)
    run_started_monotonic = time.monotonic()
    if bundle_root.exists() or bundle_root.is_symlink():
        msg = "bundle root must not already exist"
        raise InputError(msg)
    identified = identify_committed_run(
        repository_path=repository_path,
        requested_ref=requested_ref,
        resolved_commit=resolved_commit,
        run_request_path=run_request_path,
    )
    repository = GitRepository(repository_path, resolved_commit)
    repository.require_ref_commit(requested_ref)
    granted_inputs = {
        stage: stage_inputs(repository, identified, supplied, stage=stage)
        for stage in STAGES
    }
    descriptor_modes = _descriptor_modes(
        repository, composition_key_names(repository, supplied)
    )
    run_deadline = (
        run_started_monotonic
        + RUN_LIMITS.run_seconds
        - RUN_LIMITS.finalization_reserve_seconds
    )

    bundle_root.parent.mkdir(parents=True, exist_ok=True)
    claim_path = bundle_root.parent / f".{bundle_root.name}.claim"
    work_root: Path | None = None
    bundle_staging: Path | None = None
    claim_acquired = False
    try:
        try:
            claim_descriptor = os.open(
                claim_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
            )
        except FileExistsError as error:
            msg = "bundle root is already claimed by another dispatch"
            raise InputError(msg) from error
        os.close(claim_descriptor)
        claim_acquired = True
        work_root = Path(
            tempfile.mkdtemp(
                prefix=f".{bundle_root.name}.work-", dir=bundle_root.parent
            )
        )
        bundle_staging = Path(
            tempfile.mkdtemp(
                prefix=f".{bundle_root.name}.bundle-", dir=bundle_root.parent
            )
        )
        executions: list[StageExecution] = []
        outputs: dict[str, StageInputSource] = {}
        blocker: StageExecution | None = None
        for stage in STAGES:
            if blocker is None:
                execution = execute_identified_stage(
                    repository=repository,
                    identified=identified,
                    descriptor_path=STAGES[stage].local_descriptor,
                    attempt_root=work_root / stage,
                    available_inputs=granted_inputs[stage] | outputs,
                    deadline_monotonic=run_deadline,
                )
            else:
                blocked_stage = blocker.envelope.correlation.stage
                execution = skipped_stage_execution(
                    execution_id=identified["execution_id"],
                    stage=stage,
                    implementation_mode=descriptor_modes[stage],
                    code="sdi.dependency.prerequisite-blocked",
                    summary=(
                        f"The {blocked_stage} Stage outcome blocked this "
                        "dependent Stage."
                    ),
                )
            if execution.envelope.correlation.stage != stage:
                msg = "reviewed descriptor order does not match its Stage"
                raise InputError(msg)
            executions.append(execution)
            if execution.blocks_downstream:
                blocker = blocker or execution
            else:
                outputs.update(execution.outputs)

        result = assemble_bundle_staging(
            identified=identified,
            run_started_at=run_started_at,
            executions=executions,
            bundle_staging=bundle_staging,
        )
        bundle_staging.replace(bundle_root)
        return result
    finally:
        if work_root is not None:
            shutil.rmtree(work_root, ignore_errors=True)
        if bundle_staging is not None and bundle_staging.exists():
            shutil.rmtree(bundle_staging, ignore_errors=True)
        if claim_acquired:
            claim_path.unlink(missing_ok=True)
