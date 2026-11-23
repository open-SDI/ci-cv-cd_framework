"""Accepted attempts for tests, published and reloaded by the production code."""

from __future__ import annotations

import hashlib
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from sdi_pipeline_integration._json_input import canonical_json
from sdi_pipeline_integration._stage_contracts import AcceptedAttemptEnvelope
from sdi_pipeline_integration._stage_registry import (
    EXPECTED_STAGE_EVIDENCE,
    EXPECTED_STAGE_INPUT_SLOTS,
    EXPECTED_STAGE_OUTPUTS,
    SLOT_BY_NAME,
    STAGE_ORDER,
)
from sdi_pipeline_integration._stage_runtime import (
    StageExecution,
    _publish_execution,  # pyright: ignore[reportPrivateUsage]
    load_stage_execution,
    skipped_stage_execution,
)
from tests._pipeline_events import EXECUTION_ID

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from pydantic import JsonValue

    from sdi_pipeline_integration._stage_contracts import (
        DomainOutcome,
        ExecutionConclusion,
        ImplementationMode,
    )
    from sdi_pipeline_integration._stage_registry import StageName

STARTED_AT: Final = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
DURATION_MS: Final[dict[StageName, int]] = {
    "composition": 95_000,
    "image_build": 750_000,
    "cv": 1_212_000,
    "cd": 301_000,
}
type Termination = Literal["exited", "signaled", "timed_out", "lost"]


def attempt(  # noqa: PLR0913
    stage: StageName,
    *,
    mode: ImplementationMode = "implemented",
    conclusion: ExecutionConclusion = "succeeded",
    domain: DomainOutcome = "succeeded",
    reason: tuple[str, str] | None = None,
    documents: Mapping[str, Mapping[str, JsonValue]] | None = None,
    termination: Termination = "exited",
    exit_code: int = 0,
) -> StageExecution:
    """Return one completed attempt as `load_stage_execution` reads it back.

    `reason` is `(category, code)`. `documents` maps a Domain output or
    evidence slot to its JSON document.
    """
    files = {
        _path(stage, slot): canonical_json(document)
        for slot, document in (documents or {}).items()
    }
    finished_at = STARTED_AT + timedelta(milliseconds=DURATION_MS[stage])
    process: dict[str, object] = {
        "started_at": STARTED_AT,
        "finished_at": finished_at,
        "duration_ms": DURATION_MS[stage],
        "termination": termination,
    }
    if termination == "exited":
        process["exit_code"] = exit_code
    if termination == "signaled":
        process["signal"] = 9
    envelope = AcceptedAttemptEnvelope.model_validate(
        {
            "schema_version": "sdi.accepted-attempt-envelope/v1",
            "correlation": {
                "execution_id": EXECUTION_ID,
                "stage": stage,
                "attempt_number": 1,
            },
            "lifecycle_state": "completed",
            "execution_conclusion": conclusion,
            "implementation_mode": mode,
            "domain_outcome": domain,
            "adapter_response_accepted": conclusion == "succeeded",
            "process": process,
            "accepted_inputs": [
                {
                    "slot": slot,
                    "source_path": f"inputs/{SLOT_BY_NAME[slot].file_name}",
                    "schema_version": SLOT_BY_NAME[slot].schema_version,
                    "byte_size": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                }
                for slot in EXPECTED_STAGE_INPUT_SLOTS[stage]
            ],
            "accepted_files": [
                _accepted_file(stage, slot, files[_path(stage, slot)])
                for slot in (documents or {})
            ],
        }
        | (
            {}
            if reason is None
            else {
                "reason": {
                    "category": reason[0],
                    "code": reason[1],
                    "summary": f"The attempt ended with {reason[1]}.",
                }
            }
        ),
        strict=True,
        extra="forbid",
    )
    return reloaded(StageExecution(envelope=envelope, files=files, outputs={}))


def reloaded(execution: StageExecution) -> StageExecution:
    """Publish an attempt as the runtime does and load it back as Finalize does."""
    stage = execution.envelope.correlation.stage
    with tempfile.TemporaryDirectory() as parent:
        root = Path(parent) / stage
        _publish_execution(root, execution)
        return load_stage_execution(
            root, expected_execution_id=EXECUTION_ID, expected_stage=stage
        )


def _grant(stage: StageName, slot: str) -> tuple[str, str, str]:
    outputs = EXPECTED_STAGE_OUTPUTS[stage]
    return outputs[slot] if slot in outputs else EXPECTED_STAGE_EVIDENCE[stage][slot]


def _path(stage: StageName, slot: str) -> str:
    return _grant(stage, slot)[0]


def _accepted_file(stage: StageName, slot: str, content: bytes) -> dict[str, JsonValue]:
    path, media_type, schema_version = _grant(stage, slot)
    return {
        "role": (
            "domain_output"
            if slot in EXPECTED_STAGE_OUTPUTS[stage]
            else "stage_evidence"
        ),
        "slot": slot,
        "path": path,
        "media_type": media_type,
        "schema_version": schema_version,
        "byte_size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def skipped(
    stage: StageName, code: str, *, mode: ImplementationMode = "implemented"
) -> StageExecution:
    """Return the runtime's typed skip of one Stage."""
    return skipped_stage_execution(
        execution_id=EXECUTION_ID,
        stage=stage,
        implementation_mode=mode,
        code=code,
        summary=f"The Stage was skipped with {code}.",
    )


def finalized(
    held: Sequence[StageExecution], *, run_deadline_expired: bool
) -> list[StageExecution]:
    """Return the attempts Finalize assembles from the ones it loaded.

    A copy of `finalize_jenkins_run`'s skip rule: the run deadline skips the
    first missing Stage when nothing blocked, and every later Stage is
    prerequisite-blocked.
    """
    executions = list(held)
    blocker = next((item for item in executions if item.blocks_downstream), None)
    if len(executions) < len(STAGE_ORDER) and blocker is None and run_deadline_expired:
        blocker = skipped(STAGE_ORDER[len(executions)], "sdi.run.deadline-exceeded")
        executions.append(blocker)
    executions.extend(
        skipped(stage, "sdi.dependency.prerequisite-blocked")
        for stage in STAGE_ORDER[len(executions) :]
    )
    return executions
