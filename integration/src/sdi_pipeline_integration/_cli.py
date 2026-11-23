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

from ._assessment import assess_proposal_files, load_assessment_inputs
from ._bundle import deployment_manifest, validate_bundle
from ._composition_config import (
    DEFAULT_COMPOSITION_CONFIG,
    resolve_composition_settings,
)
from ._discovery import discover
from ._generation import generate_proposals
from ._github_actions import render_github_summary
from ._github_dispatch import GitHubDispatchError, dispatch_s04
from ._integration_code import StaleAgentImageError, integration_code_digest
from ._jenkins_handoff import HandoffError, handoff_jenkins
from ._jenkins_pipeline import (
    RunDeadlineExpiredError,
    attempt_allows_continuation,
    bundle_is_red,
    check_stage_wiring,
    execute_jenkins_stage,
    finalize_jenkins_run,
    preflight_jenkins_run,
    write_stage_wiring,
)
from ._local_dispatch import dispatch_local
from ._recovery_contracts import (
    load_installation_authority,
    validate_repository_authority,
)
from ._result_contracts import PipelineIntegrationResult, run_is_red
from ._run_input import identify_committed_run
from ._schemas import check_schemas, write_schemas
from ._service_descriptions import read_service_repository
from ._stage_contracts import AdapterDescriptor
from ._stage_profiles import check_stage_profiles, write_stage_profiles
from ._stage_registry import STAGE_ORDER, STAGES
from ._stage_runtime import ExternalCancellation, SuppliedInputs, execute_stage
from ._yaml_input import InputError, parse_yaml, validate_contract

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from ._stage_registry import StageName


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
    profiles = commands.add_parser(
        "stage-profiles",
        help="write or check the Stage profiles rendered from the slot registry",
    )
    profile_action = profiles.add_mutually_exclusive_group(required=True)
    profile_action.add_argument("--check", action="store_true")
    profile_action.add_argument("--write", action="store_true")
    profiles.add_argument("--directory", type=Path, default=Path("stage-profiles"))
    jenkinsfile = commands.add_parser(
        "jenkinsfile",
        help="write or check the Jenkinsfile Stage wiring rendered from the registry",
    )
    jenkinsfile_action = jenkinsfile.add_mutually_exclusive_group(required=True)
    jenkinsfile_action.add_argument("--check", action="store_true")
    jenkinsfile_action.add_argument("--write", action="store_true")
    jenkinsfile.add_argument("--jenkinsfile", type=Path, required=True)
    code_digest = commands.add_parser(
        "integration-code-digest",
        help="print the digest of the integration code an agent image bakes",
    )
    code_digest.add_argument("--root", type=Path, required=True)
    installation = commands.add_parser(
        "validate-installation",
        help="validate every tracked source against the installation authority",
    )
    installation.add_argument("--repository", type=Path, required=True)
    installation.add_argument("--installation", type=Path, required=True)
    services = commands.add_parser(
        "validate-service-repository",
        help="validate the SDI.md of every service folder under a directory",
    )
    services.add_argument("--service-repository", type=Path, required=True)
    assess = commands.add_parser(
        "assess-proposals",
        help="check and rank composition proposals against a service repository",
    )
    assess.add_argument("--service-repository", type=Path, required=True)
    assess.add_argument("--target-profile", type=Path, required=True)
    assess.add_argument("--requirements-specification", type=Path, required=True)
    assess.add_argument("--proposals", type=Path, required=True)
    generate = commands.add_parser(
        "generate-proposals",
        help="ask the configured model for composition proposals",
    )
    generate.add_argument("--service-repository", type=Path, required=True)
    generate.add_argument("--target-profile", type=Path, required=True)
    generate.add_argument("--requirements-specification", type=Path, required=True)
    generate.add_argument(
        "--composition-config",
        type=Path,
        required=True,
        help="file path of the composition config; the committed one is "
        f"{DEFAULT_COMPOSITION_CONFIG}",
    )
    _add_generator_argument(generate)
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--evidence", type=Path, required=True)
    adapter_image = commands.add_parser(
        "adapter-image",
        help="print the immutable runtime image of each Stage's Jenkins adapter "
        "descriptor, one per line in argument order",
    )
    adapter_image.add_argument("--repository", type=Path, required=True)
    adapter_image.add_argument(
        "--stage", choices=STAGE_ORDER, action="append", required=True
    )
    stage_status = commands.add_parser(
        "stage-status",
        help="print each Stage's Jenkins and local implementation mode",
    )
    stage_status.add_argument("--repository", type=Path, required=True)
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
    _add_supplied_input_arguments(execute, require_service_repository=False)
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
    preflight.add_argument("--integration-baked-code-digest", required=True)
    for flag in ("--stage-node", "--stage-baked-code-digest"):
        preflight.add_argument(
            flag,
            type=_stage_assignment,
            action="append",
            required=True,
            metavar="STAGE=VALUE",
            help="repeat once per Stage",
        )
    _add_composition_selection_arguments(preflight)
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
    jenkins_execute.add_argument("--stage", choices=STAGE_ORDER, required=True)
    jenkins_execute.add_argument("--attempt-root", type=Path, required=True)
    jenkins_execute.add_argument(
        "--prior-attempt-root", type=Path, action="append", default=[]
    )
    jenkins_execute.add_argument("--work-limit-seconds", type=int)
    jenkins_execute.add_argument("--run-deadline-epoch-millis", required=True)
    jenkins_execute.add_argument("--baked-code-digest", required=True)
    _add_composition_selection_arguments(jenkins_execute)
    attempt_status = commands.add_parser(
        "attempt-allows-continuation",
        help="derive whether an accepted attempt permits the next Stage",
    )
    attempt_status.add_argument("--attempt-root", type=Path, required=True)
    attempt_status.add_argument("--execution-id", required=True)
    attempt_status.add_argument("--stage", choices=STAGE_ORDER, required=True)
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
        help="validate a bundle and derive its Jenkins run colour",
    )
    conclusion.add_argument("--bundle-root", type=Path, required=True)
    conclusion.add_argument("--execution-id", required=True)
    manifest = commands.add_parser(
        "deployment-manifest",
        help="write the Kubernetes manifest CD applied in a valid bundle",
    )
    manifest.add_argument("--bundle-root", type=Path, required=True)
    manifest.add_argument("--output", type=Path, required=True)
    dispatch = commands.add_parser(
        "dispatch-local",
        help="execute and assemble one local four-Stage run",
    )
    dispatch.add_argument("--repository", type=Path, required=True)
    dispatch.add_argument("--requested-ref", required=True)
    dispatch.add_argument("--resolved-commit", required=True)
    dispatch.add_argument("--run-request-path", required=True)
    dispatch.add_argument("--bundle-root", type=Path, required=True)
    _add_supplied_input_arguments(dispatch, require_service_repository=True)
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


def _stage_assignment(value: str) -> tuple[StageName, str]:
    stage, separator, assigned = value.partition("=")
    if not separator or not assigned:
        msg = f"expected STAGE=VALUE, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    if stage not in STAGES:
        msg = f"unknown Stage {stage!r}; expected one of {', '.join(STAGE_ORDER)}"
        raise argparse.ArgumentTypeError(msg)
    return stage, assigned


def _one_value_per_stage(
    assignments: Sequence[tuple[StageName, str]], flag: str
) -> dict[StageName, str]:
    named = [stage for stage, _value in assignments]
    if sorted(named) != sorted(STAGE_ORDER):
        msg = f"{flag} must name each of {', '.join(STAGE_ORDER)} exactly once"
        raise InputError(msg)
    return dict(assignments)


def _add_supplied_input_arguments(
    parser: argparse.ArgumentParser, *, require_service_repository: bool
) -> None:
    parser.add_argument(
        "--service-repository",
        type=Path,
        required=require_service_repository,
        help="directory of service folders given to composition and image-build",
    )
    _add_composition_selection_arguments(parser)


def _add_composition_selection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--composition-config",
        help="repository path of the composition config, read at the resolved "
        f"commit (default {DEFAULT_COMPOSITION_CONFIG})",
    )
    _add_generator_argument(parser)


def _supplied_inputs(arguments: argparse.Namespace) -> SuppliedInputs:
    return SuppliedInputs(
        service_repository=arguments.service_repository,
        composition_config=arguments.composition_config,
        generator=arguments.generator,
    )


def _add_generator_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--generator",
        help="a generator the composition config names, instead of its default",
    )


def _write_handoff_progress(phase: str) -> None:
    sys.stderr.write(f"sdi-integration: handoff phase={phase}\n")


def _stage_status(repository: Path) -> str:
    def mode(descriptor: str) -> str:
        document = parse_yaml((repository / descriptor).read_bytes(), descriptor)
        return validate_contract(
            AdapterDescriptor, document, descriptor
        ).implementation_mode

    return "".join(
        f"{name.replace('_', '-')}: {mode(stage.jenkins_descriptor)} (jenkins), "
        f"{mode(stage.local_descriptor)} (local)\n"
        for name, stage in STAGES.items()
    )


def _write_json(path: Path, document: object) -> None:
    path.write_text(f"{json.dumps(document, indent=2, sort_keys=True)}\n")


def _generate_proposals(arguments: argparse.Namespace) -> int:
    try:
        arguments.output.unlink(missing_ok=True)
        composition = resolve_composition_settings(
            arguments.composition_config.read_bytes(),
            path=str(arguments.composition_config),
            generator=arguments.generator,
        )
        config = composition.generator_config
        inputs = load_assessment_inputs(
            service_repository=arguments.service_repository,
            target_profile=arguments.target_profile,
            requirements_specification=arguments.requirements_specification,
        )
        embeddings = composition.discovery.embeddings
        discovery = discover(
            inputs,
            embeddings=embeddings,
            max_context_bytes=config.max_context_bytes,
        )
        settings = composition.evidence_settings()
        stopped, lexical_only = discovery.stop_reason(embeddings)
        if stopped == "dense-discovery-unavailable":
            sys.stderr.write(
                f"sdi-integration: {stopped}: embeddings {discovery.dense_status}\n"
            )
        elif stopped is not None:
            sys.stderr.write(f"sdi-integration: {stopped}\n")
        if lexical_only:
            sys.stderr.write(
                f"sdi-integration: embeddings {discovery.dense_status}; "
                "discovery is lexical only\n"
            )
        if stopped is not None:
            _write_json(
                arguments.evidence,
                {
                    "settings": settings,
                    "outcome": stopped,
                    "discovery": discovery.record,
                },
            )
            return 1
        result = generate_proposals(inputs=inputs, discovery=discovery, config=config)
        evidence = {
            "settings": settings,
            "discovery": discovery.record,
            "outcome": result.outcome,
            "reason": result.reason,
        } | {
            name: value
            for name, value in (
                ("request", result.request),
                ("response", result.response),
                ("error", result.error),
            )
            if value is not None
        }
        if result.proposals is None:
            _write_json(arguments.evidence, evidence)
            sys.stderr.write(f"sdi-integration: {result.outcome}: {result.reason}\n")
            return 1
        _write_json(arguments.output, result.proposals.model_dump(mode="json"))
        _write_json(arguments.evidence, evidence)
    except (InputError, OSError, ValidationError) as error:
        sys.stderr.write(f"sdi-integration: {error}\n")
        return 2
    return 0


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
    if arguments.command == "stage-profiles":
        try:
            if arguments.write:
                write_stage_profiles(arguments.directory)
            else:
                check_stage_profiles(arguments.directory)
        except (InputError, OSError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        return 0
    if arguments.command == "jenkinsfile":
        try:
            if arguments.write:
                write_stage_wiring(arguments.jenkinsfile)
            else:
                check_stage_wiring(arguments.jenkinsfile)
        except (InputError, OSError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        return 0
    if arguments.command == "integration-code-digest":
        try:
            digest = integration_code_digest(arguments.root)
        except (InputError, OSError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        sys.stdout.write(f"{digest}\n")
        return 0
    if arguments.command == "validate-installation":
        try:
            authority = load_installation_authority(arguments.installation)
            validate_repository_authority(arguments.repository, authority)
        except (OSError, ValueError, ValidationError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        return 0
    if arguments.command == "validate-service-repository":
        try:
            files = read_service_repository(arguments.service_repository)
        except (InputError, OSError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        listed = {
            "services": [
                {
                    "path": file.path,
                    "service_id": file.description.service_id,
                    "sha256": file.sha256,
                }
                for file in files
            ]
        }
        sys.stdout.write(
            f"{json.dumps(listed, sort_keys=True, separators=(',', ':'))}\n"
        )
        return 0
    if arguments.command == "assess-proposals":
        try:
            assessment = assess_proposal_files(
                service_repository=arguments.service_repository,
                target_profile=arguments.target_profile,
                requirements_specification=arguments.requirements_specification,
                proposals=arguments.proposals,
            )
        except (InputError, OSError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        document = assessment.to_evidence()
        sys.stdout.write(
            f"{json.dumps(document, sort_keys=True, separators=(',', ':'))}\n"
        )
        return 0
    if arguments.command == "generate-proposals":
        return _generate_proposals(arguments)
    if arguments.command == "adapter-image":
        images: list[str] = []
        for stage in cast("list[StageName]", arguments.stage):
            path = arguments.repository / STAGES[stage].jenkins_descriptor
            try:
                document = parse_yaml(path.read_bytes(), str(path))
                images.append(AdapterDescriptor.model_validate(document).image)
            except (InputError, OSError) as error:
                sys.stderr.write(f"sdi-integration: {error}\n")
                return 2
            except ValidationError as error:
                sys.stderr.write(f"sdi-integration: {path}: {error}\n")
                return 2
        sys.stdout.write("".join(f"{image}\n" for image in images))
        return 0
    if arguments.command == "stage-status":
        try:
            status = _stage_status(arguments.repository)
        except (InputError, OSError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        sys.stdout.write(status)
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
                    supplied=_supplied_inputs(arguments),
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
                resource_nodes={"integration": arguments.integration_node}
                | _one_value_per_stage(arguments.stage_node, "--stage-node"),
                baked_code_digests={
                    "integration": arguments.integration_baked_code_digest
                }
                | _one_value_per_stage(
                    arguments.stage_baked_code_digest, "--stage-baked-code-digest"
                ),
                supplied=SuppliedInputs(
                    composition_config=arguments.composition_config,
                    generator=arguments.generator,
                ),
            )
        except StaleAgentImageError as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 4
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
                    stage=arguments.stage,
                    attempt_root=arguments.attempt_root,
                    prior_attempt_roots=arguments.prior_attempt_root,
                    work_limit_seconds=arguments.work_limit_seconds,
                    run_deadline_epoch_millis=arguments.run_deadline_epoch_millis,
                    baked_code_digest=arguments.baked_code_digest,
                    supplied=SuppliedInputs(
                        composition_config=arguments.composition_config,
                        generator=arguments.generator,
                    ),
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
        except StaleAgentImageError as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 4
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
            red = bundle_is_red(
                arguments.bundle_root,
                execution_id=arguments.execution_id,
            )
        except (InputError, OSError, ValidationError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        return 1 if red else 0
    if arguments.command == "deployment-manifest":
        try:
            content = deployment_manifest(arguments.bundle_root)
            if content is not None:
                with arguments.output.open("xb") as stream:
                    stream.write(content)
        except (InputError, OSError, ValidationError) as error:
            sys.stderr.write(f"sdi-integration: {error}\n")
            return 2
        return 0
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
                    supplied=_supplied_inputs(arguments),
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
        return 1 if run_is_red(PipelineIntegrationResult.model_validate(result)) else 0
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
