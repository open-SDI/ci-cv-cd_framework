"""GitHub Actions presentation over validated Pipeline integration evidence."""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from pydantic import TypeAdapter, ValidationError

from ._handoff_contracts import HandoffReceipt
from ._json_input import parse_json
from ._local_dispatch import validate_bundle
from ._result_contracts import PIPELINE_RESULT_PATH, PipelineIntegrationResult
from ._stage_contracts import ExecutionId
from ._yaml_input import InputError

if TYPE_CHECKING:
    from pathlib import Path

MAX_RECEIPT_BYTES = 64 * 1024
GITHUB_ARTIFACT_PATH = re.compile(
    r"^/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/"
    r"(?P<run_id>[1-9][0-9]*)/artifacts/[1-9][0-9]*$"
)
type StepOutcome = Literal["success", "failure", "cancelled", "skipped"]


def _load_receipt(path: Path) -> HandoffReceipt:
    if not path.is_file() or path.is_symlink():
        msg = "handoff receipt must be a regular file"
        raise InputError(msg)
    content = path.read_bytes()
    try:
        return HandoffReceipt.model_validate(
            parse_json(content, str(path), max_bytes=MAX_RECEIPT_BYTES),
            strict=True,
            extra="forbid",
        )
    except ValidationError as error:
        msg = f"handoff receipt contract validation failed: {error}"
        raise InputError(msg) from error


def _code(value: object) -> str:
    return f"`{html.escape(str(value), quote=True).replace('`', '&#96;')}`"


def _reason(attempt: object) -> str:
    reason = getattr(attempt, "reason", None)
    if reason is None:
        return "not reported"
    summary = html.escape(reason.summary, quote=True).replace("|", "\\|")
    return f"{_code(reason.code)}: {summary}"


def _evidence_paths(result: PipelineIntegrationResult, stage: str) -> str:
    paths = [f"bundle/{item.path}" for item in result.artifacts if item.stage == stage]
    if not paths:
        return "none"
    return "<br>".join(_code(path) for path in paths)


def _validate_artifact_url(artifact_url: str, github_run_id: int) -> None:
    if not artifact_url:
        return
    parsed = urlsplit(artifact_url)
    path_match = GITHUB_ARTIFACT_PATH.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or path_match is None
        or int(path_match.group("run_id")) != github_run_id
    ):
        msg = "artifact URL must identify this exact GitHub Actions run"
        raise InputError(msg)


def render_github_summary(  # noqa: C901, PLR0912, PLR0913, PLR0915
    *,
    execution_id: str,
    github_run_id: int,
    github_run_attempt: int,
    receipt_path: Path,
    bundle_root: Path,
    handoff_outcome: StepOutcome,
    publication_outcome: StepOutcome,
    artifact_name: str,
    artifact_url: str,
    workflow_cancelled: bool,
) -> tuple[str, bool]:
    """Render a status-first summary and derive Pipeline integration success."""
    try:
        TypeAdapter(ExecutionId).validate_python(execution_id, strict=True)
    except ValidationError as error:
        msg = f"invalid Execution ID: {error}"
        raise InputError(msg) from error
    if github_run_id <= 0 or github_run_attempt <= 0:
        msg = "GitHub run ID and attempt must be positive integers"
        raise InputError(msg)
    _validate_artifact_url(artifact_url, github_run_id)
    expected_artifact_name = f"pipeline-integration-{execution_id}"
    if artifact_name != expected_artifact_name:
        msg = "GitHub artifact name does not match the Execution ID"
        raise InputError(msg)

    receipt: HandoffReceipt | None = None
    receipt_state = "not returned"
    if receipt_path.exists() or receipt_path.is_symlink():
        try:
            candidate_receipt = _load_receipt(receipt_path)
        except (InputError, OSError, ValidationError):
            receipt_state = "rejected"
        else:
            if (
                candidate_receipt.execution_id == execution_id
                and candidate_receipt.github.run_id == github_run_id
                and candidate_receipt.github.run_attempt == github_run_attempt
            ):
                receipt = candidate_receipt
                receipt_state = "validated"
            else:
                receipt_state = "identity mismatch"

    artifact = _code(artifact_name)
    if artifact_url:
        artifact = f"[{artifact}]({html.escape(artifact_url, quote=True)})"
    if workflow_cancelled:
        lines = [
            "# Pipeline integration: cancelled",
            "",
            "## Run identity and provenance",
            "",
            f"- Execution ID: {_code(execution_id)}",
        ]
        if receipt is not None:
            lines.append(
                f"- GitHub run: {_code(receipt.github.run_id)}, attempt "
                f"{_code(receipt.github.run_attempt)}"
            )
        handoff_state = receipt.phase if receipt is not None else "not available"
        lines.extend(
            [
                "",
                "## Handoff",
                "",
                f"- State: {_code(handoff_state)}",
                f"- Receipt: {_code(receipt_state)}",
                f"- Handoff step: {_code(handoff_outcome)}",
                "- No Pipeline integration result is promised for cancellation.",
                "",
                "## Published evidence",
                "",
                f"- Publication: {_code(publication_outcome)}",
                f"- Artifact: {artifact}",
            ]
        )
        if receipt is not None:
            lines.append("- `handoff-receipt.json`")
        lines.extend(
            [
                "",
                "KPI evaluation: `not evaluated`",
                "",
            ]
        )
        return "\n".join(lines), True

    result: PipelineIntegrationResult | None = None
    result_state = "not returned"
    if bundle_root.exists() or bundle_root.is_symlink():
        try:
            candidate = validate_bundle(bundle_root)
        except (InputError, OSError, ValidationError):
            result_state = "rejected"
        else:
            if candidate.execution_id == execution_id:
                result = candidate
                result_state = "validated"
            else:
                result_state = "identity mismatch"

    succeeded = (
        result is not None
        and receipt is not None
        and handoff_outcome == "success"
        and publication_outcome == "success"
        and receipt.phase == "completed"
        and receipt.terminal is not None
        and receipt.terminal.outcome == "succeeded"
    )
    status = "succeeded" if succeeded else "failed"
    queue = (
        receipt.queue.relative_url
        if receipt is not None and receipt.queue is not None
        else "not available"
    )
    build = (
        receipt.build.relative_url
        if receipt is not None and receipt.build is not None
        else "not available"
    )
    lines = [
        f"# Pipeline integration: {status}",
        "",
        "## Run identity and provenance",
        "",
        f"- Execution ID: {_code(execution_id)}",
    ]
    if receipt is not None:
        lines.append(
            f"- GitHub run: {_code(receipt.github.run_id)}, attempt "
            f"{_code(receipt.github.run_attempt)}"
        )
    if result is not None:
        lines.extend(
            [
                f"- Scenario: {_code(result.scenario_id)}",
                f"- Testcase: {_code(result.testcase_id)}",
                f"- Combination: {_code(result.combination_id)}",
                f"- Target execution profile: {_code(result.profile_id)}",
                f"- Repository: {_code(result.repository)}",
                f"- Requested ref: {_code(result.requested_ref)}",
                f"- Resolved commit: {_code(result.resolved_commit_sha)}",
            ]
        )
    handoff_state = receipt.phase if receipt is not None else "not available"
    lines.extend(
        [
            "",
            "## Handoff",
            "",
            f"- State: {_code(handoff_state)}",
            f"- Receipt: {_code(receipt_state)}",
            f"- Result: {_code(result_state)}",
            f"- Jenkins queue reference: {_code(queue)}",
            f"- Jenkins build reference: {_code(build)}",
        ]
    )
    terminal = receipt.terminal if receipt is not None else None
    if terminal is not None and terminal.outcome == "failed":
        lines.append(f"- Failure code: {_code(terminal.code)}")
    if terminal is not None and terminal.jenkins_result is not None:
        lines.append(f"- Jenkins result: {_code(terminal.jenkins_result)}")
    if result is not None:
        lines.extend(
            [
                "",
                "## Stage facts",
                "",
                (
                    "| Stage | Lifecycle | Execution | Implementation | "
                    "Domain outcome | Typed explanation | Evidence paths |"
                ),
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for attempt in result.attempts:
            stage = attempt.correlation.stage
            lines.append(
                f"| {_code(stage.replace('_', '-'))} "
                f"| {_code(attempt.lifecycle_state)} "
                f"| {_code(attempt.execution_conclusion)} "
                f"| {_code(attempt.implementation_mode)} "
                f"| {_code(attempt.domain_outcome)} "
                f"| {_reason(attempt)} "
                f"| {_evidence_paths(result, stage)} |"
            )
    lines.extend(
        [
            "",
            "## Published evidence",
            "",
            f"- Publication: {_code(publication_outcome)}",
            f"- Artifact: {artifact}",
        ]
    )
    if receipt is not None:
        lines.append("- `handoff-receipt.json`")
    if result is not None:
        lines.extend(
            [
                f"- {_code(f'bundle/{PIPELINE_RESULT_PATH}')}",
                "",
                "## Evidence limits",
                "",
                f"Fixture warning: {html.escape(result.fixture_notice, quote=True)}",
            ]
        )
    lines.extend(["", "KPI evaluation: `not evaluated`", ""])
    return "\n".join(lines), succeeded
