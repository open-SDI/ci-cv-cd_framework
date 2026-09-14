"""Command-line entry point for the pipeline integration scaffold."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from ._github_actions import render_github_summary
from ._github_dispatch import GitHubDispatchError, dispatch_s04
from ._jenkins_handoff import HandoffError, handoff_jenkins
from ._jenkins_pipeline import (
    RunDeadlineExpiredError,
    attempt_allows_continuation,
    bundle_has_machinery_failure,
    execute_jenkins_stage,
    finalize_jenkins_run,
    preflight_jenkins_run,
)
from ._local_dispatch import dispatch_local, validate_bundle
from ._recovery_contracts import (
    load_installation_authority,
    validate_repository_authority,
)
from ._run_input import identify_committed_run
from ._schemas import check_schemas, write_schemas
from ._stage_contracts import AdapterDescriptor
from ._stage_runtime import ExternalCancellation, execute_stage
from ._yaml_input import InputError, parse_yaml

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence


@contextmanager
def _external_cancellation() -> Generator[None, None, None]:
    previous: dict[signal.Signals, Any] = {}  # pyright: ignore[reportExplicitAny]

    def cancel(signal_number: int, _frame: object) -> None:
        raise ExternalCancellation(signal_number)

    for cancellation_signal in (signal.SIGINT, signal.SIGTERM):
        previous[cancellation_signal] = signal.signal(cancellation_signal, cancel)
    try:
        yield
    finally:
        for cancellation_signal, handler in previous.items():
            signal.signal(cancellation_signal, handler)


def _parser() -> argparse.ArgumentParser:  # noqa: PLR0915
    parser = argparse.ArgumentParser(
        prog="sdi-integration",
        description="Validate and execute Pipeline integration run requests.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('sdi-pipeline-integration')}",
    )
    commands = parser.add_subparsers(dest="command")
    identify = commands.add_parser(
        "identify-run",
        help="validate committed inputs and assign an Execution ID",
    )
    identify.add_argument("--repository", type=Path, required=True)
    identify.add_argument("--requested-ref", required=True)
    identify.add_argument("--resolved-commit", required=True)
    identify.add_argument("--run-request-path", required=True)
    schemas = commands.add_parser(
        "schemas",
        help="write or check generated input contract schemas",
    )
    schema_action = schemas.add_mutually_exclusive_group(required=True)
    schema_action.add_argument("--check", action="store_true")
    schema_action.add_argument("--write", action="store_true")
    schemas.add_argument("--directory", type=Path, default=Path("schemas"))
    installation = commands.add_parser(
        "validate-installation",
        help="validate every tracked source against the installation authority",
    )
    installation.add_argument("--repository", type=Path, required=True)
    installation.add_argument("--installation", type=Path, required=True)
    adapter_image = commands.add_parser(
        "adapter-image",
        help="print the immutable runtime image selected by an adapter descriptor",
    )
    adapter_image.add_argument("--descriptor", type=Path, required=True)
    execute = commands.add_parser(
        "execute-stage",
        help="execute and transactionally accept one trusted Stage adapter",
    )
    execute.add_argument("--repository", type=Path, required=True)
    execute.add_argument("--requested-ref", required=True)
    execute.add_argument("--resolved-commit", required=True)
    execute.add_argument("--run-request-path", required=True)
    execute.add_argument("--descriptor-path", required=True)
    execute.add_argument("--attempt-root", type=Path, required=True)
    preflight = commands.add_parser(
        "preflight-jenkins-run",
        help="revalidate one Jenkins submission and its complete execution topology",
    )
    _add_submitted_run_arguments(preflight)
    preflight.add_argument("--handoff-contract-version", required=True)
    preflight.add_argument("--github-run-id", required=True)
    preflight.add_argument("--github-run-attempt", required=True)
    preflight.add_argument("--build-started-at-millis", required=True)
    preflight.add_argument("--integration-node", required=True)
    preflight.add_argument("--composition-node", required=True)
    preflight.add_argument("--image-build-node", required=True)
    preflight.add_argument("--cv-node", required=True)
    preflight.add_argument("--cd-node", required=True)
    handoff = commands.add_parser(
        "handoff-jenkins",
        help="submit one run and retrieve its exact validated Jenkins bundle",
    )
    _add_submitted_run_arguments(handoff)
    handoff.add_argument("--github-run-id", required=True)
    handoff.add_argument("--github-run-attempt", required=True)
    handoff.add_argument("--bundle-root", type=Path, required=True)
    handoff.add_argument("--receipt-path", type=Path, required=True)
    jenkins_execute = commands.add_parser(
        "execute-jenkins-stage",
        help="execute the next Stage from validated Jenkins transfers",
    )
    _add_submitted_run_arguments(jenkins_execute)
    jenkins_execute.add_argument("--descriptor-path", required=True)
    jenkins_execute.add_argument("--attempt-root", type=Path, required=True)
    jenkins_execute.add_argument(
        "--prior-attempt-root", type=Path, action="append", default=[]
    )
    jenkins_execute.add_argument("--work-limit-seconds", type=int, required=True)
    jenkins_execute.add_argument("--run-deadline-epoch-millis", required=True)
    attempt_status = commands.add_parser(
        "attempt-allows-continuation",
        help="derive whether an accepted attempt permits the next Stage",
    )
    attempt_status.add_argument("--attempt-root", type=Path, required=True)
    attempt_status.add_argument("--execution-id", required=True)
    attempt_status.add_argument(
        "--stage", choices=("composition", "image_build", "cv", "cd"), required=True
    )
    finalize = commands.add_parser(
        "finalize-jenkins-run",
        help="materialize skips and assemble a complete Jenkins result bundle",
    )
    _add_submitted_run_arguments(finalize)
    finalize.add_argument("--build-started-at-millis", required=True)
    finalize.add_argument("--attempt-root", type=Path, action="append", default=[])
    finalize.add_argument("--run-deadline-expired", action="store_true")
    finalize.add_argument("--bundle-root", type=Path, required=True)
    conclusion = commands.add_parser(
        "bundle-conclusion",
        help="validate a bundle and derive its Jenkins machinery conclusion",
    )
    conclusion.add_argument("--bundle-root", type=Path, required=True)
    conclusion.add_argument("--execution-id", required=True)
    dispatch = commands.add_parser(
        "dispatch-local",
        help="execute and assemble one local four-Stage run",
    )
    dispatch.add_argument("--repository", type=Path, required=True)
    dispatch.add_argument("--requested-ref", required=True)
    dispatch.add_argument("--resolved-commit", required=True)
    dispatch.add_argument("--run-request-path", required=True)
    dispatch.add_argument("--bundle-root", type=Path, required=True)
    validate = commands.add_parser(
        "validate-bundle",
        help="validate one complete Pipeline integration archive candidate",
    )
    validate.add_argument("--bundle-root", type=Path, required=True)
    summary = commands.add_parser(
        "github-summary",
        help="validate published evidence and render the final GitHub summary",
    )
    summary.add_argument("--execution-id", required=True)
    summary.add_argument("--github-run-id", type=int, required=True)
    summary.add_argument("--github-run-attempt", type=int, required=True)
    summary.add_argument("--receipt-path", type=Path, required=True)
    summary.add_argument("--bundle-root", type=Path, required=True)
    summary.add_argument(
        "--handoff-outcome",
        choices=("success", "failure", "cancelled", "skipped"),
        required=True,
    )
    summary.add_argument(
        "--publication-outcome",
        choices=("success", "failure", "cancelled", "skipped"),
        required=True,
    )
    summary.add_argument("--artifact-name", required=True)
    summary.add_argument("--artifact-url", default="")
    summary.add_argument("--workflow-cancelled", action="store_true")
    s04 = commands.add_parser(
        "dispatch-s-04",
        help="sequentially dispatch the six reviewed S-04 Fixture requests",
    )
    s04.add_argument("--repository", required=True)
    return parser


def _add_submitted_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--requested-ref", required=True)
    parser.add_argument("--resolved-commit", required=True)
    parser.add_argument("--run-request-path", required=True)
    parser.add_argument("--execution-id", required=True)


def _write_handoff_progress(phase: str) -> None:
    sys.stderr.write(f"sdi-integration: handoff phase={phase}\n")


def _has_machinery_failure(result: dict[str, object]) -> bool:
    attempts = result.get("attempts")
    if not isinstance(attempts, list):
        return True
    for raw_attempt in cast("list[object]", attempts):
        if not isinstance(raw_attempt, dict):
            return True
        attempt = cast("dict[str, object]", raw_attempt)
        if attempt.get("execution_conclusion") in {"failed", "timed_out"}:
            return True
        raw_reason = attempt.get("reason")
        if isinstance(raw_reason, dict):
            reason = cast("dict[str, object]", raw_reason)
            if reason.get("code") in {
                "sdi.stage.not-implemented",
                "sdi.dependency.fixture-evidence",
                "sdi.run.deadline-exceeded",
            }:
                return True
    return False


def main(  # noqa: C901, PLR0911, PLR0912, PLR0915
    argv: Sequence[str] | None = None,
) -> int:
    """Run the public command-line interface."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "identify-run":
        try:
            identified = identify_committed_run(
                repository_path=arguments.repository,
                requested_ref=arguments.requested_ref,
                resolved_commit=arguments.resolved_commit,
                run_request_path=arguments.run_request_path,
            )
        except (InputError, OSError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        sys.stdout.write(
            f"{json.dumps(identified, sort_keys=True, separators=(',', ':'))}\n"
        )
        return 0
    if arguments.command == "schemas":
        try:
            if arguments.write:
                write_schemas(arguments.directory)
            else:
                check_schemas(arguments.directory)
        except (InputError, OSError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        return 0
    if arguments.command == "validate-installation":
        try:
            authority = load_installation_authority(arguments.installation)
            validate_repository_authority(arguments.repository, authority)
        except (OSError, ValueError, ValidationError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        return 0
    if arguments.command == "adapter-image":
        try:
            document = parse_yaml(
                arguments.descriptor.read_bytes(), str(arguments.descriptor)
            )
            descriptor = AdapterDescriptor.model_validate(document)
        except (InputError, OSError, ValidationError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        sys.stdout.write(f"{descriptor.image}\n")
        return 0
    if arguments.command == "execute-stage":
        attempt_existed = arguments.attempt_root.exists() or (
            arguments.attempt_root.is_symlink()
        )
        try:
            with _external_cancellation():
                envelope = execute_stage(
                    repository_path=arguments.repository,
                    requested_ref=arguments.requested_ref,
                    resolved_commit=arguments.resolved_commit,
                    run_request_path=arguments.run_request_path,
                    descriptor_path=arguments.descriptor_path,
                    attempt_root=arguments.attempt_root,
                )
        except ExternalCancellation as cancellation:
            if not attempt_existed:
                if arguments.attempt_root.is_symlink():
                    arguments.attempt_root.unlink(missing_ok=True)
                else:
                    shutil.rmtree(arguments.attempt_root, ignore_errors=True)
            return 128 + cancellation.signal_number
        except (InputError, OSError, ValidationError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        sys.stdout.write(
            f"{json.dumps(envelope, sort_keys=True, separators=(',', ':'))}\n"
        )
        return 0
    if arguments.command == "preflight-jenkins-run":
        try:
            identified = preflight_jenkins_run(
                repository_path=arguments.repository,
                requested_ref=arguments.requested_ref,
                resolved_commit=arguments.resolved_commit,
                run_request_path=arguments.run_request_path,
                execution_id=arguments.execution_id,
                handoff_contract_version=arguments.handoff_contract_version,
                github_run_id=arguments.github_run_id,
                github_run_attempt=arguments.github_run_attempt,
                build_started_at_millis=arguments.build_started_at_millis,
                resource_nodes={
                    "integration": arguments.integration_node,
                    "composition": arguments.composition_node,
                    "image_build": arguments.image_build_node,
                    "cv": arguments.cv_node,
                    "cd": arguments.cd_node,
                },
            )
        except (InputError, OSError, ValidationError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        sys.stdout.write(
            f"{json.dumps(identified, sort_keys=True, separators=(',', ':'))}\n"
        )
        return 0
    if arguments.command == "handoff-jenkins":
        try:
            with _external_cancellation():
                result = handoff_jenkins(
                    repository_path=arguments.repository,
                    requested_ref=arguments.requested_ref,
                    resolved_commit=arguments.resolved_commit,
                    run_request_path=arguments.run_request_path,
                    execution_id=arguments.execution_id,
                    github_run_id=arguments.github_run_id,
                    github_run_attempt=arguments.github_run_attempt,
                    bundle_root=arguments.bundle_root,
                    receipt_path=arguments.receipt_path,
                    environment=os.environ,
                    progress=_write_handoff_progress,
                )
        except ExternalCancellation as cancellation:
            return 128 + cancellation.signal_number
        except HandoffError as error:
            if error.result is not None:
                sys.stdout.write(
                    f"{json.dumps(error.result, sort_keys=True, separators=(',', ':'))}\n"  # noqa: E501
                )
            sys.stderr.write(f"sdi-integration: handoff failed: {error.code}\n")
            return 1
        except (InputError, OSError, ValidationError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        sys.stdout.write(
            f"{json.dumps(result, sort_keys=True, separators=(',', ':'))}\n"
        )
        return 0
    if arguments.command == "execute-jenkins-stage":
        attempt_existed = arguments.attempt_root.exists() or (
            arguments.attempt_root.is_symlink()
        )
        try:
            with _external_cancellation():
                envelope = execute_jenkins_stage(
                    repository_path=arguments.repository,
                    requested_ref=arguments.requested_ref,
                    resolved_commit=arguments.resolved_commit,
                    run_request_path=arguments.run_request_path,
                    execution_id=arguments.execution_id,
                    descriptor_path=arguments.descriptor_path,
                    attempt_root=arguments.attempt_root,
                    prior_attempt_roots=arguments.prior_attempt_root,
                    work_limit_seconds=arguments.work_limit_seconds,
                    run_deadline_epoch_millis=arguments.run_deadline_epoch_millis,
                )
        except ExternalCancellation as cancellation:
            if not attempt_existed:
                if arguments.attempt_root.is_symlink():
                    arguments.attempt_root.unlink(missing_ok=True)
                else:
                    shutil.rmtree(arguments.attempt_root, ignore_errors=True)
            return 128 + cancellation.signal_number
        except RunDeadlineExpiredError:
            return 3
        except (InputError, OSError, ValidationError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        sys.stdout.write(
            f"{json.dumps(envelope, sort_keys=True, separators=(',', ':'))}\n"
        )
        return 0
    if arguments.command == "attempt-allows-continuation":
        try:
            allows_continuation = attempt_allows_continuation(
                arguments.attempt_root,
                execution_id=arguments.execution_id,
                stage=arguments.stage,
            )
        except (InputError, OSError, ValidationError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        return 0 if allows_continuation else 1
    if arguments.command == "finalize-jenkins-run":
        try:
            result = finalize_jenkins_run(
                repository_path=arguments.repository,
                requested_ref=arguments.requested_ref,
                resolved_commit=arguments.resolved_commit,
                run_request_path=arguments.run_request_path,
                execution_id=arguments.execution_id,
                build_started_at_millis=arguments.build_started_at_millis,
                attempt_roots=arguments.attempt_root,
                run_deadline_expired=arguments.run_deadline_expired,
                bundle_root=arguments.bundle_root,
            )
        except (InputError, OSError, ValidationError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        sys.stdout.write(
            f"{json.dumps(result, sort_keys=True, separators=(',', ':'))}\n"
        )
        return 0
    if arguments.command == "bundle-conclusion":
        try:
            machinery_failure = bundle_has_machinery_failure(
                arguments.bundle_root,
                execution_id=arguments.execution_id,
            )
        except (InputError, OSError, ValidationError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        return 1 if machinery_failure else 0
    if arguments.command == "dispatch-local":
        bundle_existed = arguments.bundle_root.exists() or (
            arguments.bundle_root.is_symlink()
        )
        try:
            with _external_cancellation():
                result = dispatch_local(
                    repository_path=arguments.repository,
                    requested_ref=arguments.requested_ref,
                    resolved_commit=arguments.resolved_commit,
                    run_request_path=arguments.run_request_path,
                    bundle_root=arguments.bundle_root,
                )
        except ExternalCancellation as cancellation:
            if not bundle_existed:
                if arguments.bundle_root.is_symlink():
                    arguments.bundle_root.unlink(missing_ok=True)
                else:
                    shutil.rmtree(arguments.bundle_root, ignore_errors=True)
            return 128 + cancellation.signal_number
        except (InputError, OSError, ValidationError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        sys.stdout.write(
            f"{json.dumps(result, sort_keys=True, separators=(',', ':'))}\n"
        )
        if _has_machinery_failure(result):
            return 1
        return 0
    if arguments.command == "validate-bundle":
        try:
            validate_bundle(arguments.bundle_root)
        except (InputError, OSError, ValidationError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        return 0
    if arguments.command == "github-summary":
        try:
            summary, succeeded = render_github_summary(
                execution_id=arguments.execution_id,
                github_run_id=arguments.github_run_id,
                github_run_attempt=arguments.github_run_attempt,
                receipt_path=arguments.receipt_path,
                bundle_root=arguments.bundle_root,
                handoff_outcome=arguments.handoff_outcome,
                publication_outcome=arguments.publication_outcome,
                artifact_name=arguments.artifact_name,
                artifact_url=arguments.artifact_url,
                workflow_cancelled=arguments.workflow_cancelled,
            )
        except (InputError, OSError, ValidationError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        sys.stdout.write(summary)
        return 0 if succeeded else 1
    if arguments.command == "dispatch-s-04":
        try:
            succeeded = dispatch_s04(
                arguments.repository,
                url_output=sys.stdout,
                diagnostics=sys.stderr,
            )
        except GitHubDispatchError as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 1
        except InputError as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        return 0 if succeeded else 1
    parser.print_help()
    return 0
