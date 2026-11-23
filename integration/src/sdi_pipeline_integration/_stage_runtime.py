"""Deep module for one transactional Stage-adapter attempt."""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, get_args

from pydantic import BaseModel, ValidationError

from ._composition_config import (
    DEFAULT_COMPOSITION_CONFIG,
    CompositionSettings,
    resolve_composition_settings,
)
from ._contracts import (
    MobilityRequirementsSpecification,
    RunRequest,
    TargetExecutionProfile,
)
from ._domain_contracts import (
    CompositionBlueprint,
    CompositionEvidence,
    DeploymentResult,
    DeploymentSchema,
    ImageBuildResult,
    ValidationEvidence,
)
from ._git_input import CommittedBlob, GitRepository, validate_repository_path
from ._jenkins_agent_boundary import enforce_domain_execution_boundary
from ._json_input import canonical_json, parse_json
from ._result_contracts import (
    MAX_TRANSFER_DIAGNOSTIC_BYTES,
    MAX_TRANSFER_DOMAIN_BYTES,
    MAX_TRANSFER_EVIDENCE_BYTES,
)
from ._run_input import PROTECTED_MAIN_REF, identify_committed_run
from ._service_descriptions import (
    SERVICE_REPOSITORY_ROOT,
    ServiceRepositoryManifest,
    capture_service_repository,
)
from ._stage_contracts import (
    ASCII_CONTROL_LIMIT,
    ASCII_DELETE,
    AcceptedAttemptEnvelope,
    AdapterDescriptor,
    AdapterRequest,
    AdapterResponse,
    DomainOutcome,
    ImplementationMode,
    StageProfile,
    contains_sensitive_contract_material,
    contains_sensitive_material,
    render_stage_profile,
    require_unique_paths,
)
from ._stage_documents import validate_stage_documents
from ._stage_profiles import stage_profile_bytes
from ._stage_registry import (
    COMMITTED_SLOTS,
    EXPECTED_STAGE_EVIDENCE,
    EXPECTED_STAGE_INPUT_SLOTS,
    EXPECTED_STAGE_OUTPUTS,
    SLOT_BY_NAME,
    SLOT_BY_SCHEMA,
    STAGES,
    StageName,
)
from ._yaml_input import (
    InputError,
    parse_front_matter,
    parse_yaml,
    validate_contract,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

MAX_RESPONSE_BYTES = 64 * 1024
MAX_ACCEPTED_ENVELOPE_BYTES = 64 * 1024
MAX_CANDIDATE_ENTRIES = 32
MAX_CANDIDATE_DEPTH = 4
ADAPTER_SHUTDOWN_GRACE_SECONDS = 10
# Network settings an implemented adapter may need to reach its model endpoint.
FORWARDED_NETWORK_SETTINGS = ("HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE")


@dataclass(frozen=True)
class StageInputSource:
    """Immutable accepted bytes available to a later Stage."""

    source_path: str
    media_type: str
    schema_version: str
    content: bytes
    producer_implementation_mode: ImplementationMode | None = None
    producer_domain_outcome: DomainOutcome | None = None
    # Files written beside the input that its content names, such as the
    # descriptions a service-repository manifest lists.
    attachments: tuple[tuple[str, bytes], ...] = ()


@dataclass(frozen=True)
class StageExecution:
    """One accepted attempt plus the exact files available for assembly."""

    envelope: AcceptedAttemptEnvelope
    files: dict[str, bytes]
    outputs: dict[str, StageInputSource]

    @property
    def blocks_downstream(self) -> bool:
        """Whether this attempt stops every later Stage from running."""
        envelope = self.envelope
        return (
            envelope.lifecycle_state == "skipped"
            or envelope.execution_conclusion != "succeeded"
            or envelope.domain_outcome == "failed"
            or envelope.implementation_mode == "not_implemented"
        )


class ExternalCancellation(BaseException):
    """External signal requiring immediate result-less cleanup."""

    def __init__(self, signal_number: int) -> None:
        self.signal_number = signal_number
        super().__init__(f"externally cancelled by signal {signal_number}")


def _schema_version(model: type[BaseModel]) -> str:
    (version,) = get_args(model.model_fields["schema_version"].annotation)
    return version


# Each parse model names its slot through its own schema version.
SLOT_MODELS: dict[str, type[BaseModel]] = {
    SLOT_BY_SCHEMA[_schema_version(model)].name: model
    for model in (
        RunRequest,
        MobilityRequirementsSpecification,
        TargetExecutionProfile,
        ServiceRepositoryManifest,
        CompositionSettings,
        CompositionBlueprint,
        DeploymentSchema,
        ImageBuildResult,
        ValidationEvidence,
        DeploymentResult,
    )
}
INPUT_MODELS: dict[str, type[BaseModel]] = {
    SLOT_BY_NAME[name].schema_version: model for name, model in SLOT_MODELS.items()
}
OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    **{
        SLOT_BY_NAME[name].schema_version: model
        for name, model in SLOT_MODELS.items()
        if SLOT_BY_NAME[name].origin not in {"committed", "supplied"}
    },
    _schema_version(CompositionEvidence): CompositionEvidence,
}


def _validate_yaml_model[ModelT: BaseModel](
    model: type[ModelT], blob: CommittedBlob
) -> ModelT:
    return validate_contract(model, parse_yaml(blob.content, blob.path), blob.path)


def _validate_source_model[ModelT: BaseModel](
    model: type[ModelT], source: StageInputSource
) -> ModelT:
    if source.media_type == "application/yaml":
        parsed = parse_yaml(source.content, source.source_path)
    elif source.media_type == "text/markdown":
        parsed, _body = parse_front_matter(source.content, source.source_path)
    elif source.media_type == "application/json":
        parsed = parse_json(
            source.content,
            source.source_path,
            max_bytes=max(1, len(source.content)),
        )
    else:
        msg = "Stage input uses an unsupported media type"
        raise InputError(msg)
    return validate_contract(model, parsed, source.source_path)


def _write_file(root: Path, relative_path: str, content: bytes) -> None:
    validate_repository_path(relative_path)
    destination = root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def capture_tree(  # noqa: C901, PLR0915
    root: Path,
    expected_root_identity: tuple[int, int],
    grants: Mapping[str, int],
) -> tuple[set[str], dict[str, bytes]]:
    paths: set[str] = set()
    folded_paths: set[str] = set()
    captured: dict[str, bytes] = {}
    flags = os.O_RDONLY | os.O_CLOEXEC
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = flags | os.O_DIRECTORY | no_follow
    try:
        root_descriptor = os.open(root, directory_flags)
    except OSError as error:
        msg = "candidate bundle root was replaced"
        raise InputError(msg) from error
    root_metadata = os.fstat(root_descriptor)
    if (root_metadata.st_dev, root_metadata.st_ino) != expected_root_identity:
        os.close(root_descriptor)
        msg = "candidate bundle root was replaced"
        raise InputError(msg)

    def walk(  # noqa: C901, PLR0912, PLR0915
        directory_descriptor: int, prefix: str, depth: int
    ) -> None:
        if depth > MAX_CANDIDATE_DEPTH:
            msg = "candidate bundle directory depth exceeds its limit"
            raise InputError(msg)
        for name in os.listdir(directory_descriptor):
            relative = f"{prefix}/{name}" if prefix else name
            validate_repository_path(relative)
            folded = relative.casefold()
            if folded in folded_paths:
                msg = "candidate bundle contains case-colliding paths"
                raise InputError(msg)
            folded_paths.add(folded)
            paths.add(relative)
            if len(paths) > MAX_CANDIDATE_ENTRIES:
                msg = "candidate bundle contains too many entries"
                raise InputError(msg)
            metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            if metadata.st_dev != root_metadata.st_dev:
                msg = "candidate bundle crosses a filesystem device"
                raise InputError(msg)
            if stat.S_ISLNK(metadata.st_mode):
                msg = "candidate bundle contains a symbolic link"
                raise InputError(msg)
            if stat.S_ISDIR(metadata.st_mode):
                child_descriptor = os.open(
                    name,
                    directory_flags,
                    dir_fd=directory_descriptor,
                )
                try:
                    opened = os.fstat(child_descriptor)
                    if (opened.st_dev, opened.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        msg = "candidate bundle changed during traversal"
                        raise InputError(msg)
                    walk(child_descriptor, relative, depth + 1)
                finally:
                    os.close(child_descriptor)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                msg = "candidate bundle contains a non-regular file"
                raise InputError(msg)
            if metadata.st_nlink != 1:
                msg = "candidate bundle contains a hard-linked file"
                raise InputError(msg)
            limit = grants.get(relative)
            if limit is None:
                msg = "candidate bundle contains undeclared files"
                raise InputError(msg)
            if metadata.st_size > limit:
                msg = f"candidate bundle file exceeds its {limit}-byte grant"
                raise InputError(msg)
            file_descriptor = os.open(
                name,
                flags | no_follow | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_descriptor,
            )
            try:
                before = os.fstat(file_descriptor)
                if (before.st_dev, before.st_ino) != (
                    metadata.st_dev,
                    metadata.st_ino,
                ):
                    msg = "candidate bundle changed during capture"
                    raise InputError(msg)
                content = b""
                while len(content) <= limit:
                    chunk = os.read(
                        file_descriptor,
                        min(65536, limit + 1 - len(content)),
                    )
                    if not chunk:
                        break
                    content += chunk
                after = os.fstat(file_descriptor)
            finally:
                os.close(file_descriptor)
            stable_fields = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            if any(
                getattr(before, field) != getattr(after, field)
                for field in stable_fields
            ):
                msg = "candidate bundle changed while being captured"
                raise InputError(msg)
            if len(content) != before.st_size or len(content) > limit:
                msg = "candidate bundle file changed size while being captured"
                raise InputError(msg)
            captured[relative] = content

    try:
        walk(root_descriptor, "", 1)
    finally:
        os.close(root_descriptor)
    return paths, captured


def expected_directories(files: set[str]) -> set[str]:
    directories: set[str] = set()
    for path in files:
        parent = Path(path).parent
        while parent != Path():
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _capture_candidate(  # noqa: C901, PLR0912, PLR0913, PLR0915, PLR0917
    output_root: Path,
    output_root_identity: tuple[int, int],
    request: AdapterRequest,
    implementation_mode: str,
    identified: Mapping[str, Any],
    input_models: Mapping[str, BaseModel],
    secrets: tuple[bytes, ...] = (),
) -> tuple[AdapterResponse, dict[str, bytes]]:
    grants = {item.path: item.max_bytes for item in request.outputs}
    grants[request.diagnostic.path] = request.diagnostic.max_bytes
    grants["response.json"] = MAX_RESPONSE_BYTES
    inventory_paths, captured = capture_tree(
        output_root,
        output_root_identity,
        grants,
    )
    if "response.json" not in captured:
        msg = "candidate bundle did not publish response.json"
        raise InputError(msg)
    if any(secret in content for content in captured.values() for secret in secrets):
        msg = "candidate bundle contains a forwarded secret"
        raise InputError(msg)
    try:
        response = AdapterResponse.model_validate(
            parse_json(
                captured["response.json"],
                "response.json",
                max_bytes=MAX_RESPONSE_BYTES,
            ),
            strict=True,
            extra="forbid",
        )
    except ValidationError as error:
        msg = f"candidate response contract validation failed: {error}"
        raise InputError(msg) from error
    if response.correlation != request.correlation:
        msg = "candidate response correlation does not match the request"
        raise InputError(msg)
    if implementation_mode == "fixture" and (
        response.domain_outcome == "succeeded"
        or response.reason is None
        or (
            response.domain_outcome == "not_evaluated"
            and response.reason.category != "fixture"
        )
    ):
        msg = "Fixture response must preserve its evidence limits"
        raise InputError(msg)

    input_slots = [item.slot for item in request.inputs]
    output_by_slot = {item.slot: item for item in request.outputs}
    if not set(response.consumed_inputs) <= set(input_slots):
        msg = "candidate response names an undeclared input slot"
        raise InputError(msg)
    if not set(response.produced_outputs) <= set(output_by_slot):
        msg = "candidate response names an ungranted output slot"
        raise InputError(msg)
    required_outputs = {
        item.slot for item in request.outputs if item.role == "domain_output"
    }
    if response.execution_conclusion == "succeeded":
        if set(response.consumed_inputs) != set(input_slots):
            msg = "candidate response did not consume every declared input"
            raise InputError(msg)
        if response.domain_outcome == "failed":
            if required_outputs & set(response.produced_outputs):
                msg = "candidate negative Domain outcome produced ungranted output"
                raise InputError(msg)
        elif not required_outputs <= set(response.produced_outputs):
            msg = "candidate response did not produce every required output"
            raise InputError(msg)
    expected_files = {
        "response.json",
        *(output_by_slot[slot].path for slot in response.produced_outputs),
    }
    if response.diagnostic.present:
        expected_files.add(request.diagnostic.path)
    expected_paths = expected_files | expected_directories(expected_files)
    if inventory_paths != expected_paths:
        msg = "candidate bundle inventory does not match its response"
        raise InputError(msg)

    validated_outputs: dict[str, BaseModel] = {}
    for slot in response.produced_outputs:
        grant = output_by_slot[slot]
        model = OUTPUT_MODELS.get(grant.schema_version)
        if model is None:
            msg = "candidate output uses an unsupported schema version"
            raise InputError(msg)
        try:
            validated_outputs[slot] = model.model_validate(
                parse_json(captured[grant.path], grant.path, max_bytes=grant.max_bytes),
                strict=True,
                extra="forbid",
            )
        except ValidationError as error:
            msg = f"candidate output contract validation failed: {slot}: {error}"
            raise InputError(msg) from error
        # Evidence carries the exact model request, whose service descriptions
        # name public upstream URLs; the forwarded-secret check above covers it.
        if grant.role == "domain_output" and contains_sensitive_contract_material(
            validated_outputs[slot].model_dump(mode="json")
        ):
            msg = (
                f"candidate output contains credentials, private endpoints, or "
                f"machine-specific values: {slot}"
            )
            raise InputError(msg)
    if any(
        getattr(output, "evidence_basis", None) != implementation_mode
        for output in validated_outputs.values()
    ):
        msg = "candidate output evidence basis does not match the reviewed adapter"
        raise InputError(msg)

    if response.diagnostic.present:
        try:
            diagnostic = captured[request.diagnostic.path].decode(
                "utf-8", errors="strict"
            )
        except UnicodeDecodeError as error:
            msg = "candidate diagnostic is not valid UTF-8"
            raise InputError(msg) from error
        if "\0" in diagnostic or any(
            (ord(character) < ASCII_CONTROL_LIMIT and character not in "\t\r\n")
            or ord(character) == ASCII_DELETE
            for character in diagnostic
        ):
            msg = "candidate diagnostic contains unsanitized control characters"
            raise InputError(msg)
        if contains_sensitive_material(diagnostic):
            msg = "candidate diagnostic contains credentials or private endpoints"
            raise InputError(msg)

    if response.execution_conclusion == "succeeded":
        validate_stage_documents(
            request.correlation.stage,
            {
                slot: output
                for slot, output in validated_outputs.items()
                if output_by_slot[slot].role == "domain_output"
            },
            input_models,
            {item.slot: item.sha256 for item in request.inputs},
            (
                identified["scenario_id"],
                identified["testcase_id"],
                identified["combination_id"],
                identified["profile_id"],
            ),
            domain_outcome=response.domain_outcome,
            evidence=[
                output
                for slot, output in validated_outputs.items()
                if output_by_slot[slot].role == "stage_evidence"
            ],
        )
    second_paths, second_capture = capture_tree(
        output_root,
        output_root_identity,
        grants,
    )
    if second_paths != inventory_paths or second_capture != captured:
        msg = "candidate bundle changed after capture"
        raise InputError(msg)
    return response, captured


def _accepted_envelope(  # noqa: PLR0913
    *,
    descriptor: AdapterDescriptor,
    request: AdapterRequest,
    response: AdapterResponse,
    input_sources: Mapping[str, StageInputSource],
    captured: Mapping[str, bytes],
    started_at: datetime,
    finished_at: datetime,
) -> AcceptedAttemptEnvelope:
    accepted_files: list[dict[str, object]] = []
    for grant in sorted(request.outputs, key=lambda item: item.role != "domain_output"):
        if grant.slot not in response.produced_outputs:
            continue
        content = captured[grant.path]
        accepted_files.append(
            {
                "role": grant.role,
                "slot": grant.slot,
                "path": grant.path,
                "media_type": grant.media_type,
                "schema_version": grant.schema_version,
                "byte_size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    if response.diagnostic.present:
        content = captured[request.diagnostic.path]
        accepted_files.append(
            {
                "role": "diagnostic",
                "slot": "diagnostic",
                "path": request.diagnostic.path,
                "media_type": request.diagnostic.media_type,
                "byte_size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "truncated": response.diagnostic.truncated,
            }
        )
    envelope_data: dict[str, object] = {
        "schema_version": "sdi.accepted-attempt-envelope/v1",
        "correlation": request.correlation.model_dump(mode="json"),
        "lifecycle_state": "completed",
        "execution_conclusion": response.execution_conclusion,
        "implementation_mode": descriptor.implementation_mode,
        "domain_outcome": response.domain_outcome,
        "adapter_response_accepted": True,
        "process": {
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": max(
                0, int((finished_at - started_at).total_seconds() * 1000)
            ),
            "termination": "exited",
            "exit_code": 0,
        },
        "accepted_inputs": _accepted_inputs(request, input_sources),
        "accepted_files": accepted_files,
    }
    if response.reason is not None:
        envelope_data["reason"] = response.reason.model_dump(mode="json")
    return AcceptedAttemptEnvelope.model_validate(
        envelope_data,
        strict=True,
        extra="forbid",
    )


def _accepted_inputs(
    request: AdapterRequest, input_sources: Mapping[str, StageInputSource]
) -> list[dict[str, object]]:
    return [
        {
            "slot": declared.slot,
            "source_path": input_sources[declared.slot].source_path,
            "schema_version": input_sources[declared.slot].schema_version,
            "byte_size": len(input_sources[declared.slot].content),
            "sha256": hashlib.sha256(input_sources[declared.slot].content).hexdigest(),
        }
        for declared in request.inputs
    ]


def _runtime_failure_execution(  # noqa: PLR0913
    *,
    descriptor: AdapterDescriptor,
    request: AdapterRequest,
    input_sources: Mapping[str, StageInputSource],
    started_at: datetime,
    finished_at: datetime,
    conclusion: str,
    termination: str,
    code: str,
    summary: str,
    exit_code: int | None = None,
    signal_number: int | None = None,
) -> StageExecution:
    process: dict[str, object] = {
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": max(0, int((finished_at - started_at).total_seconds() * 1000)),
        "termination": termination,
    }
    if exit_code is not None:
        process["exit_code"] = exit_code
    if signal_number is not None:
        process["signal"] = signal_number
    envelope = AcceptedAttemptEnvelope.model_validate(
        {
            "schema_version": "sdi.accepted-attempt-envelope/v1",
            "correlation": request.correlation.model_dump(mode="json"),
            "lifecycle_state": "completed",
            "execution_conclusion": conclusion,
            "implementation_mode": descriptor.implementation_mode,
            "domain_outcome": "not_evaluated",
            "reason": {
                "category": "adapter",
                "code": code,
                "summary": summary,
            },
            "adapter_response_accepted": False,
            "process": process,
            "accepted_inputs": _accepted_inputs(request, input_sources),
            "accepted_files": [],
        },
        strict=True,
        extra="forbid",
    )
    return StageExecution(envelope=envelope, files={}, outputs={})


def skipped_stage_execution(
    *,
    execution_id: str,
    stage: StageName,
    implementation_mode: ImplementationMode,
    code: str,
    summary: str,
) -> StageExecution:
    """Create one assembler-owned typed skip without claiming process work."""
    envelope = AcceptedAttemptEnvelope.model_validate(
        {
            "schema_version": "sdi.accepted-attempt-envelope/v1",
            "correlation": {
                "execution_id": execution_id,
                "stage": stage,
                "attempt_number": 1,
            },
            "lifecycle_state": "skipped",
            "execution_conclusion": "skipped",
            "implementation_mode": implementation_mode,
            "domain_outcome": "not_evaluated",
            "reason": {
                "category": "dependency",
                "code": code,
                "summary": summary,
            },
            "adapter_response_accepted": False,
            "accepted_inputs": [],
            "accepted_files": [],
        },
        strict=True,
        extra="forbid",
    )
    return StageExecution(envelope=envelope, files={}, outputs={})


def _publish_execution(attempt_root: Path, execution: StageExecution) -> None:
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{attempt_root.name}.accepted-", dir=attempt_root.parent
        )
    )
    try:
        for relative_path, content in execution.files.items():
            _write_file(staging, relative_path, content)
        _write_file(
            staging,
            "accepted-attempt.json",
            canonical_json(
                execution.envelope.model_dump(mode="json", exclude_none=True)
            ),
        )
        staging.replace(attempt_root)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _materialize_stage_inputs(
    profile: StageProfile,
    input_root: Path,
    available_inputs: Mapping[str, StageInputSource],
    implementation_mode: ImplementationMode,
) -> tuple[list[dict[str, object]], dict[str, BaseModel]]:
    required_slots = {item.slot for item in profile.inputs}
    selected_sources = {
        slot: source
        for slot, source in available_inputs.items()
        if slot in required_slots
    }
    if set(selected_sources) != required_slots:
        msg = "Stage profile input grants do not match accepted run inputs"
        raise InputError(msg)
    declared_inputs: list[dict[str, object]] = []
    validated_inputs: dict[str, BaseModel] = {}
    for grant in profile.inputs:
        source = selected_sources[grant.slot]
        if (
            implementation_mode == "implemented"
            and source.producer_implementation_mode == "fixture"
            and source.producer_domain_outcome == "not_evaluated"
        ):
            msg = "implemented adapter cannot consume unevaluated Fixture output"
            raise InputError(msg)
        model = INPUT_MODELS.get(grant.schema_version)
        if (
            model is None
            or source.schema_version != grant.schema_version
            or source.media_type != grant.media_type
        ):
            msg = "Stage input grant does not match an accepted input slot"
            raise InputError(msg)
        validated_inputs[grant.slot] = _validate_source_model(model, source)
        digest = hashlib.sha256(source.content).hexdigest()
        _write_file(input_root, grant.path, source.content)
        for relative_path, content in source.attachments:
            _write_file(input_root, relative_path, content)
        declared_inputs.append(
            {
                **grant.model_dump(mode="json"),
                "byte_size": len(source.content),
                "sha256": digest,
            }
        )
    return declared_inputs, validated_inputs


def committed_stage_sources(
    repository: GitRepository, identified: Mapping[str, Any]
) -> dict[str, StageInputSource]:
    """Revalidate and capture the immutable committed run inputs."""
    sources: dict[str, StageInputSource] = {}
    for provenance in identified["inputs"]:
        slot = SLOT_BY_SCHEMA.get(provenance["schema_version"])
        if slot is None or slot.origin != "committed":
            msg = "identified run contains an unsupported input contract"
            raise InputError(msg)
        blob = repository.read_regular_file(provenance["path"])
        digest = hashlib.sha256(blob.content).hexdigest()
        if (
            len(blob.content) != provenance["byte_size"]
            or digest != provenance["sha256"]
        ):
            msg = "accepted input provenance changed before Stage execution"
            raise InputError(msg)
        source = StageInputSource(
            source_path=blob.path,
            media_type=slot.media_type,
            schema_version=slot.schema_version,
            content=blob.content,
        )
        _validate_source_model(SLOT_MODELS[slot.name], source)
        sources[slot.name] = source
    if list(sources) != [slot.name for slot in COMMITTED_SLOTS]:
        msg = "identified run does not contain its exact committed input set"
        raise InputError(msg)
    return sources


def _supplied_source(
    name: str, content: bytes, attachments: tuple[tuple[str, bytes], ...] = ()
) -> StageInputSource:
    slot = SLOT_BY_NAME[name]
    return StageInputSource(
        source_path=slot.file_name,
        media_type=slot.media_type,
        schema_version=slot.schema_version,
        content=content,
        attachments=attachments,
    )


def service_repository_source(root: Path) -> StageInputSource:
    """Capture every description under a supplied directory and its manifest."""
    snapshot = capture_service_repository(root)
    return _supplied_source(
        "service_repository",
        canonical_json(snapshot.manifest.model_dump(mode="json")),
        tuple(
            (f"{SERVICE_REPOSITORY_ROOT}/{path}", content)
            for path, content in snapshot.files.items()
        ),
    )


@dataclass(frozen=True)
class SuppliedInputs:
    """The operator-supplied inputs a run names; only granted Stages read them.

    An omitted composition config selects the committed default.
    """

    service_repository: Path | None = None
    composition_config: str | None = None
    generator: str | None = None


def _composition_config(
    repository: GitRepository, supplied: SuppliedInputs
) -> CommittedBlob:
    path = supplied.composition_config or DEFAULT_COMPOSITION_CONFIG
    validate_repository_path(path)
    return repository.read_regular_file(path)


def composition_key_names(
    repository: GitRepository, supplied: SuppliedInputs
) -> tuple[str, ...]:
    """Name the model keys the run's composition settings use."""
    blob = _composition_config(repository, supplied)
    return resolve_composition_settings(
        blob.content, path=blob.path, generator=supplied.generator
    ).key_names


def _resolve_supplied_slot(
    repository: GitRepository, supplied: SuppliedInputs, name: str, stage: StageName
) -> StageInputSource:
    if name == "service_repository":
        if supplied.service_repository is None:
            msg = f"the {stage} Stage requires a supplied service repository"
            raise InputError(msg)
        return service_repository_source(supplied.service_repository)
    if name == "composition_settings":
        blob = _composition_config(repository, supplied)
        return composition_settings_source(
            blob.content, path=blob.path, generator=supplied.generator
        )
    msg = f"no resolver for the supplied input slot {name}"
    raise InputError(msg)


def stage_inputs(
    repository: GitRepository,
    identified: Mapping[str, Any],
    supplied: SuppliedInputs,
    *,
    stage: StageName,
) -> dict[str, StageInputSource]:
    """Capture only the committed and supplied inputs granted to one Stage.

    Supplied inputs are resolved here and nowhere else, so a Stage that is not
    granted the service repository never reads it.
    """
    committed = committed_stage_sources(repository, identified)
    sources: dict[str, StageInputSource] = {}
    for name in EXPECTED_STAGE_INPUT_SLOTS[stage]:
        origin = SLOT_BY_NAME[name].origin
        if origin == "committed":
            sources[name] = committed[name]
        elif origin == "supplied":
            sources[name] = _resolve_supplied_slot(repository, supplied, name, stage)
    return sources


def reject_ungranted_supplied_inputs(
    supplied: SuppliedInputs, *, stage: StageName
) -> None:
    """Reject a supplied input that one Stage's command names but is not granted.

    Local dispatch supplies one run's inputs to every Stage, so only commands
    that execute a single Stage call this.
    """
    granted = EXPECTED_STAGE_INPUT_SLOTS[stage]
    named = {
        "service repository": (
            "service_repository",
            supplied.service_repository is not None,
        ),
        "composition config": (
            "composition_settings",
            supplied.composition_config is not None,
        ),
        "generator": ("composition_settings", supplied.generator is not None),
    }
    for label, (slot, given) in named.items():
        if given and slot not in granted:
            msg = f"the {stage} Stage is not granted a supplied {label}"
            raise InputError(msg)


def composition_settings_source(
    content: bytes, *, path: str, generator: str | None
) -> StageInputSource:
    """Capture the settings a composition config's bytes resolve to."""
    settings = resolve_composition_settings(content, path=path, generator=generator)
    return _supplied_source(
        "composition_settings",
        canonical_json(settings.model_dump(mode="json", exclude_none=True)),
    )


def check_key_bindings(
    descriptor: AdapterDescriptor, key_names: Sequence[str]
) -> tuple[str, ...]:
    """Require the descriptor to bind every model key the run uses; name them.

    Only a Stage granted the composition settings receives their keys. Preflight
    calls this for every Stage, so a key the descriptor does not bind rejects
    the run before any Stage starts.
    """
    if (
        "composition_settings" not in EXPECTED_STAGE_INPUT_SLOTS[descriptor.stage]
        or not descriptor.secret_bindings
    ):
        return ()
    if not set(key_names) <= set(descriptor.secret_bindings):
        msg = "composition settings name a key the adapter descriptor does not bind"
        raise InputError(msg)
    return tuple(key_names)


def _read_accepted_envelope(
    attempt_root: Path, expected_root_identity: tuple[int, int]
) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    root_descriptor = os.open(attempt_root, flags | os.O_DIRECTORY | no_follow)
    try:
        root_metadata = os.fstat(root_descriptor)
        if (root_metadata.st_dev, root_metadata.st_ino) != expected_root_identity:
            msg = "accepted attempt root was replaced"
            raise InputError(msg)
        envelope_descriptor = os.open(
            "accepted-attempt.json",
            flags | no_follow,
            dir_fd=root_descriptor,
        )
        try:
            before = os.fstat(envelope_descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_dev != root_metadata.st_dev
                or before.st_size > MAX_ACCEPTED_ENVELOPE_BYTES
            ):
                msg = "accepted attempt envelope is not a bounded regular file"
                raise InputError(msg)
            content = b""
            while len(content) <= MAX_ACCEPTED_ENVELOPE_BYTES:
                chunk = os.read(
                    envelope_descriptor,
                    min(65536, MAX_ACCEPTED_ENVELOPE_BYTES + 1 - len(content)),
                )
                if not chunk:
                    break
                content += chunk
            after = os.fstat(envelope_descriptor)
        finally:
            os.close(envelope_descriptor)
    except FileNotFoundError as error:
        msg = "accepted attempt is missing its envelope"
        raise InputError(msg) from error
    finally:
        os.close(root_descriptor)
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if len(content) != before.st_size or any(
        getattr(before, field) != getattr(after, field) for field in stable_fields
    ):
        msg = "accepted attempt envelope changed while being read"
        raise InputError(msg)
    return content


def load_stage_execution(  # noqa: C901, PLR0912, PLR0915
    attempt_root: Path,
    *,
    expected_execution_id: str,
    expected_stage: StageName,
) -> StageExecution:
    """Validate and reconstruct one Jenkins-transferred accepted attempt."""
    if not attempt_root.is_dir() or attempt_root.is_symlink():
        msg = "accepted attempt root must be a directory"
        raise InputError(msg)
    root_metadata = attempt_root.lstat()
    root_identity = (root_metadata.st_dev, root_metadata.st_ino)
    envelope_content = _read_accepted_envelope(attempt_root, root_identity)
    try:
        envelope = AcceptedAttemptEnvelope.model_validate(
            parse_json(
                envelope_content,
                "accepted-attempt.json",
                max_bytes=MAX_ACCEPTED_ENVELOPE_BYTES,
            ),
            strict=True,
            extra="forbid",
        )
    except ValidationError as error:
        msg = f"accepted attempt envelope validation failed: {error}"
        raise InputError(msg) from error
    if (
        envelope.correlation.execution_id != expected_execution_id
        or envelope.correlation.stage != expected_stage
        or envelope.correlation.attempt_number != 1
    ):
        msg = "accepted attempt correlation does not match the requested transfer"
        raise InputError(msg)

    accepted_paths = [item.path for item in envelope.accepted_files]
    try:
        require_unique_paths(accepted_paths)
    except ValueError as error:
        msg = f"accepted attempt file inventory is invalid: {error}"
        raise InputError(msg) from error
    accepted_slots = [item.slot for item in envelope.accepted_files]
    if len(accepted_slots) != len(set(accepted_slots)):
        msg = "accepted attempt file slots must be unique"
        raise InputError(msg)
    if envelope.lifecycle_state == "completed" and [
        item.slot for item in envelope.accepted_inputs
    ] != list(EXPECTED_STAGE_INPUT_SLOTS[expected_stage]):
        msg = "accepted attempt inputs do not match the reviewed Stage graph"
        raise InputError(msg)
    domain_files = [
        item for item in envelope.accepted_files if item.role == "domain_output"
    ]
    evidence_files = [
        item for item in envelope.accepted_files if item.role == "stage_evidence"
    ]
    if any(
        (item.path, item.media_type, item.schema_version)
        != EXPECTED_STAGE_EVIDENCE[expected_stage].get(item.slot)
        or item.byte_size > MAX_TRANSFER_EVIDENCE_BYTES
        for item in evidence_files
    ) or (evidence_files and envelope.execution_conclusion != "succeeded"):
        msg = "accepted attempt evidence does not match the reviewed Stage grant"
        raise InputError(msg)
    if (
        envelope.execution_conclusion == "succeeded"
        and envelope.domain_outcome != "failed"
    ):
        expected_outputs = EXPECTED_STAGE_OUTPUTS[expected_stage]
        if [item.slot for item in domain_files] != list(expected_outputs):
            msg = "accepted attempt outputs do not match the reviewed Stage graph"
            raise InputError(msg)
        for accepted in domain_files:
            expected_path, expected_media_type, expected_schema = expected_outputs[
                accepted.slot
            ]
            if (
                accepted.path,
                accepted.media_type,
                accepted.schema_version,
            ) != (expected_path, expected_media_type, expected_schema):
                msg = "accepted attempt output does not match the reviewed Stage grant"
                raise InputError(msg)
    elif domain_files:
        msg = "unsuccessful accepted attempt cannot transfer Domain output"
        raise InputError(msg)
    if any(item.byte_size > MAX_TRANSFER_DOMAIN_BYTES for item in domain_files):
        msg = "accepted attempt Domain output exceeds its transfer limit"
        raise InputError(msg)
    diagnostics = [
        item for item in envelope.accepted_files if item.role == "diagnostic"
    ]
    if len(diagnostics) > 1 or any(
        item.path != "diagnostic.txt" or item.byte_size > MAX_TRANSFER_DIAGNOSTIC_BYTES
        for item in diagnostics
    ):
        msg = "accepted attempt diagnostic does not match its transfer grant"
        raise InputError(msg)

    grants = {"accepted-attempt.json": MAX_ACCEPTED_ENVELOPE_BYTES}
    grants.update({item.path: item.byte_size for item in envelope.accepted_files})
    paths, captured = capture_tree(attempt_root, root_identity, grants)
    expected_files = {
        "accepted-attempt.json",
        *(item.path for item in envelope.accepted_files),
    }
    if paths != expected_files | expected_directories(expected_files):
        msg = "accepted attempt inventory does not match its envelope"
        raise InputError(msg)
    if captured["accepted-attempt.json"] != envelope_content:
        msg = "accepted attempt envelope changed during validation"
        raise InputError(msg)

    files: dict[str, bytes] = {}
    outputs: dict[str, StageInputSource] = {}
    for accepted in envelope.accepted_files:
        content = captured[accepted.path]
        if (
            len(content) != accepted.byte_size
            or hashlib.sha256(content).hexdigest() != accepted.sha256
        ):
            msg = f"accepted attempt file does not match its envelope: {accepted.path}"
            raise InputError(msg)
        files[accepted.path] = content
        if accepted.role == "diagnostic":
            try:
                diagnostic = content.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                msg = "accepted attempt diagnostic is not valid UTF-8"
                raise InputError(msg) from error
            if "\0" in diagnostic or any(
                (ord(character) < ASCII_CONTROL_LIMIT and character not in "\t\r\n")
                or ord(character) == ASCII_DELETE
                for character in diagnostic
            ):
                msg = "accepted attempt diagnostic is not sanitized"
                raise InputError(msg)
            if contains_sensitive_material(diagnostic):
                msg = "accepted attempt diagnostic contains sensitive material"
                raise InputError(msg)
            continue
        model = OUTPUT_MODELS.get(accepted.schema_version)
        if model is None:
            msg = "accepted attempt output uses an unsupported schema"
            raise InputError(msg)
        source = StageInputSource(
            source_path=(
                f"stages/{expected_stage.replace('_', '-')}/"
                f"{accepted.path.removeprefix('outputs/')}"
            ),
            media_type=accepted.media_type,
            schema_version=accepted.schema_version,
            content=content,
            producer_implementation_mode=envelope.implementation_mode,
            producer_domain_outcome=envelope.domain_outcome,
        )
        validated = _validate_source_model(model, source)
        if getattr(validated, "evidence_basis", None) != envelope.implementation_mode:
            msg = "accepted attempt evidence basis does not match its envelope"
            raise InputError(msg)
        if accepted.role == "stage_evidence":
            continue
        if contains_sensitive_contract_material(validated.model_dump(mode="json")):
            msg = "accepted attempt output contains sensitive material"
            raise InputError(msg)
        outputs[accepted.slot] = source

    second_paths, second_capture = capture_tree(attempt_root, root_identity, grants)
    if second_paths != paths or second_capture != captured:
        msg = "accepted attempt changed after validation"
        raise InputError(msg)
    return StageExecution(envelope=envelope, files=files, outputs=outputs)


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    return True


def _candidate_rejection_reason(message: str) -> tuple[str, str]:  # noqa: PLR0911
    lowered = message.lower()
    if "did not publish response.json" in lowered:
        return (
            "sdi.adapter.response-absent",
            "The Stage adapter did not publish a candidate response.",
        )
    if "response contract" in lowered or "valid json" in lowered:
        return (
            "sdi.adapter.response-malformed",
            "The Stage adapter published a malformed candidate response.",
        )
    # Contract failures quote the offending input, which may contain any of the
    # words matched below, so they are classified first.
    if "contract validation" in lowered or "unsupported schema" in lowered:
        return (
            "sdi.adapter.schema-mismatch",
            "The candidate output did not satisfy its reviewed contract.",
        )
    if "correlation" in lowered or "identity" in lowered:
        return (
            "sdi.adapter.identity-mismatch",
            "The candidate identity did not match the Stage request.",
        )
    if "undeclared" in lowered or "ungranted" in lowered:
        return (
            "sdi.adapter.undeclared-output",
            "The candidate contained output outside its reviewed grants.",
        )
    if any(
        marker in lowered
        for marker in ("symbolic link", "hard-linked", "non-regular", "device")
    ):
        return (
            "sdi.adapter.unsafe-output",
            "The candidate contained an unsafe filesystem object.",
        )
    if "exceeds" in lowered:
        return (
            "sdi.adapter.size-violation",
            "The candidate exceeded a reviewed byte or tree limit.",
        )
    if "required output" in lowered or "inventory" in lowered:
        return (
            "sdi.adapter.output-missing",
            "The candidate inventory did not contain exactly its declared files.",
        )
    if "changed" in lowered or "replaced" in lowered:
        return (
            "sdi.adapter.response-race",
            "The candidate changed during transactional validation.",
        )
    return (
        "sdi.adapter.candidate-rejected",
        "The complete Stage candidate transaction was rejected.",
    )


def _terminate_process_group(
    process: subprocess.Popen[bytes], process_group: int, *, immediate: bool = False
) -> None:
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    if not immediate:
        deadline = time.monotonic() + ADAPTER_SHUTDOWN_GRACE_SECONDS
        # An unreaped adapter stays a zombie member and keeps its group alive.
        while (
            process.poll() is None or _process_group_exists(process_group)
        ) and time.monotonic() < deadline:
            time.sleep(0.05)
    if _process_group_exists(process_group):
        with suppress(ProcessLookupError):
            os.killpg(process_group, signal.SIGKILL)
    if process.poll() is None:
        process.wait()


def execute_stage(  # noqa: PLR0913
    *,
    repository_path: Path,
    requested_ref: str,
    resolved_commit: str,
    run_request_path: str,
    descriptor_path: str,
    attempt_root: Path,
    supplied: SuppliedInputs,
) -> dict[str, Any]:
    """Execute and transactionally accept one descriptor-selected Stage attempt."""
    if requested_ref != PROTECTED_MAIN_REF:
        msg = f"requested ref must be {PROTECTED_MAIN_REF}"
        raise InputError(msg)
    identified = identify_committed_run(
        repository_path=repository_path,
        requested_ref=requested_ref,
        resolved_commit=resolved_commit,
        run_request_path=run_request_path,
    )
    repository = GitRepository(repository_path, resolved_commit)
    repository.require_ref_commit(requested_ref)
    descriptor, _profile = load_stage_adapter(repository, descriptor_path)
    reject_ungranted_supplied_inputs(supplied, stage=descriptor.stage)
    execution = execute_identified_stage(
        repository=repository,
        identified=identified,
        descriptor_path=descriptor_path,
        attempt_root=attempt_root,
        available_inputs=stage_inputs(
            repository, identified, supplied, stage=descriptor.stage
        ),
    )
    return execution.envelope.model_dump(mode="json", exclude_none=True)


def load_stage_adapter(
    repository: GitRepository, descriptor_path: str
) -> tuple[AdapterDescriptor, StageProfile]:
    """Load one descriptor and the Stage profile rendered from the registry.

    The committed profile must equal the rendering byte for byte, so a hand
    edit fails here as well as in the freshness check.
    """
    validate_repository_path(descriptor_path)
    descriptor_blob = repository.read_regular_file(descriptor_path)
    descriptor = _validate_yaml_model(AdapterDescriptor, descriptor_blob)
    profile_blob = repository.read_regular_file(descriptor.stage_profile)
    if profile_blob.content != stage_profile_bytes(descriptor.stage):
        msg = (
            f"{descriptor.stage_profile} does not match the {descriptor.stage} "
            "profile rendered from the registry; run "
            "sdi-integration stage-profiles --write"
        )
        raise InputError(msg)
    return descriptor, render_stage_profile(descriptor.stage)


def _adapter_environment(
    descriptor: AdapterDescriptor, available_inputs: Mapping[str, StageInputSource]
) -> tuple[dict[str, str], tuple[bytes, ...]]:
    """Build the adapter's environment, forwarding only its bound model keys."""
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LC_ALL": "C.UTF-8",
    }
    settings = available_inputs.get("composition_settings")
    key_names = check_key_bindings(
        descriptor,
        ()
        if settings is None
        else _validate_source_model(CompositionSettings, settings).key_names,
    )
    if not key_names:
        return environment, ()
    environment.update(
        {
            name: os.environ[name]
            for name in FORWARDED_NETWORK_SETTINGS
            if name in os.environ
        }
    )
    secrets: list[bytes] = []
    for name in key_names:
        api_key = os.environ.get(name)
        if api_key:
            environment[name] = api_key
            secrets.append(api_key.encode())
    return environment, tuple(secrets)


def execute_identified_stage(  # noqa: C901, PLR0911, PLR0912, PLR0913, PLR0915
    *,
    repository: GitRepository,
    identified: Mapping[str, Any],
    descriptor_path: str,
    attempt_root: Path,
    available_inputs: Mapping[str, StageInputSource],
    work_limit_seconds: int | None = None,
    deadline_monotonic: float | None = None,
) -> StageExecution:
    """Execute one Stage while preserving an already assigned Execution ID."""
    if attempt_root.exists():
        msg = "attempt root must not already exist"
        raise InputError(msg)
    descriptor, profile = load_stage_adapter(repository, descriptor_path)
    enforce_domain_execution_boundary(
        expected_label=STAGES[descriptor.stage].agent.label
    )
    effective_work_limit_seconds = (
        profile.work_limit_seconds if work_limit_seconds is None else work_limit_seconds
    )
    if effective_work_limit_seconds <= 0:
        msg = "Stage work limit must be a positive integer"
        raise InputError(msg)

    attempt_root.parent.mkdir(parents=True, exist_ok=True)
    if descriptor.implementation_mode == "not_implemented":
        execution = skipped_stage_execution(
            execution_id=identified["execution_id"],
            stage=descriptor.stage,
            implementation_mode="not_implemented",
            code="sdi.stage.not-implemented",
            summary="No reviewed implementation is available for this Stage.",
        )
        _publish_execution(attempt_root, execution)
        return execution
    if descriptor.implementation_mode == "implemented" and any(
        source.producer_implementation_mode == "fixture"
        and source.producer_domain_outcome == "not_evaluated"
        for source in available_inputs.values()
    ):
        execution = skipped_stage_execution(
            execution_id=identified["execution_id"],
            stage=descriptor.stage,
            implementation_mode="implemented",
            code="sdi.dependency.fixture-evidence",
            summary="Implemented work cannot consume unevaluated Fixture output.",
        )
        _publish_execution(attempt_root, execution)
        return execution
    if descriptor.implementation_mode == "fixture" and any(
        source.producer_implementation_mode == "implemented"
        for source in available_inputs.values()
    ):
        # Fixture cases match exact digests, so they can never recognize real
        # output; running one would only report a Fixture input mismatch.
        execution = skipped_stage_execution(
            execution_id=identified["execution_id"],
            stage=descriptor.stage,
            implementation_mode="fixture",
            code="sdi.dependency.implemented-evidence",
            summary="Fixture work cannot consume implemented output.",
        )
        _publish_execution(attempt_root, execution)
        return execution

    work_root = Path(
        tempfile.mkdtemp(prefix=f".{attempt_root.name}.work-", dir=attempt_root.parent)
    )
    process: subprocess.Popen[bytes] | None = None
    try:
        input_root = work_root / "inputs"
        output_root = work_root / "candidate"
        input_root.mkdir()
        output_root.mkdir()
        output_root_metadata = output_root.lstat()
        output_root_identity = (
            output_root_metadata.st_dev,
            output_root_metadata.st_ino,
        )
        declared_inputs, input_models = _materialize_stage_inputs(
            profile,
            input_root,
            available_inputs,
            descriptor.implementation_mode,
        )
        request = AdapterRequest.model_validate(
            {
                "schema_version": "sdi.stage-adapter-request/v1",
                "correlation": {
                    "execution_id": identified["execution_id"],
                    "stage": descriptor.stage,
                    "attempt_number": 1,
                },
                "work_limit_seconds": effective_work_limit_seconds,
                "inputs": declared_inputs,
                "outputs": [item.model_dump(mode="json") for item in profile.outputs],
                "diagnostic": profile.diagnostic.model_dump(mode="json"),
            },
            strict=True,
            extra="forbid",
        )
        request_path = work_root / "request.json"
        _write_file(
            work_root,
            "request.json",
            canonical_json(request.model_dump(mode="json")),
        )
        environment, secrets = _adapter_environment(descriptor, available_inputs)
        started_at = datetime.now(UTC)
        try:
            process = subprocess.Popen(  # noqa: S603
                [
                    descriptor.entrypoint,
                    "run",
                    "--request",
                    str(request_path),
                    "--input-root",
                    str(input_root),
                    "--output-root",
                    str(output_root),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
                start_new_session=True,
            )
        except OSError:
            execution = _runtime_failure_execution(
                descriptor=descriptor,
                request=request,
                input_sources=available_inputs,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                conclusion="failed",
                termination="lost",
                code="sdi.adapter.process-lost",
                summary="The Stage adapter process could not be started.",
            )
            _publish_execution(attempt_root, execution)
            return execution
        process_group = process.pid
        timeout_seconds = float(effective_work_limit_seconds)
        timeout_code = "sdi.stage.deadline-exceeded"
        timeout_summary = "The Stage adapter exceeded its work limit."
        if deadline_monotonic is not None:
            remaining = max(0.0, deadline_monotonic - time.monotonic())
            if remaining < timeout_seconds:
                timeout_seconds = remaining
                timeout_code = "sdi.run.deadline-exceeded"
                timeout_summary = "The Pipeline integration run deadline expired."
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except ExternalCancellation:
            _terminate_process_group(process, process_group, immediate=True)
            raise
        except subprocess.TimeoutExpired:
            _terminate_process_group(process, process_group)
            execution = _runtime_failure_execution(
                descriptor=descriptor,
                request=request,
                input_sources=available_inputs,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                conclusion="timed_out",
                termination="timed_out",
                code=timeout_code,
                summary=timeout_summary,
            )
            _publish_execution(attempt_root, execution)
            return execution
        finished_at = datetime.now(UTC)
        if _process_group_exists(process_group):
            _terminate_process_group(process, process_group)
            finished_at = datetime.now(UTC)
            signaled = return_code < 0
            execution = _runtime_failure_execution(
                descriptor=descriptor,
                request=request,
                input_sources=available_inputs,
                started_at=started_at,
                finished_at=finished_at,
                conclusion="failed",
                termination="signaled" if signaled else "exited",
                exit_code=None if signaled else return_code,
                signal_number=-return_code if signaled else None,
                code="sdi.adapter.descendants-survived",
                summary="The Stage adapter left descendant processes after exit.",
            )
            _publish_execution(attempt_root, execution)
            return execution
        if return_code != 0:
            signaled = return_code < 0
            execution = _runtime_failure_execution(
                descriptor=descriptor,
                request=request,
                input_sources=available_inputs,
                started_at=started_at,
                finished_at=finished_at,
                conclusion="failed",
                termination="signaled" if signaled else "exited",
                exit_code=None if signaled else return_code,
                signal_number=-return_code if signaled else None,
                code=(
                    "sdi.adapter.process-signaled"
                    if signaled
                    else "sdi.adapter.nonzero-exit"
                ),
                summary=(
                    "The Stage adapter process was terminated by a signal."
                    if signaled
                    else "The Stage adapter process exited unsuccessfully."
                ),
            )
            _publish_execution(attempt_root, execution)
            return execution
        try:
            response, captured = _capture_candidate(
                output_root,
                output_root_identity,
                request,
                descriptor.implementation_mode,
                identified,
                input_models,
                secrets,
            )
        except (InputError, OSError) as error:
            code, summary = _candidate_rejection_reason(str(error))
            execution = _runtime_failure_execution(
                descriptor=descriptor,
                request=request,
                input_sources=available_inputs,
                started_at=started_at,
                finished_at=finished_at,
                conclusion="failed",
                termination="exited",
                exit_code=0,
                code=code,
                summary=summary,
            )
            _publish_execution(attempt_root, execution)
            return execution
        envelope = _accepted_envelope(
            descriptor=descriptor,
            request=request,
            response=response,
            input_sources=available_inputs,
            captured=captured,
            started_at=started_at,
            finished_at=finished_at,
        )

        output_by_slot = {
            item.slot: item for item in request.outputs if item.role == "domain_output"
        }
        output_sources = {
            slot: StageInputSource(
                source_path=(
                    f"stages/{descriptor.stage.replace('_', '-')}/"
                    f"{output_by_slot[slot].path.removeprefix('outputs/')}"
                ),
                media_type=output_by_slot[slot].media_type,
                schema_version=output_by_slot[slot].schema_version,
                content=captured[output_by_slot[slot].path],
                producer_implementation_mode=descriptor.implementation_mode,
                producer_domain_outcome=response.domain_outcome,
            )
            for slot in response.produced_outputs
            if slot in output_by_slot
        }
        accepted_files = {
            item.path: captured[item.path] for item in envelope.accepted_files
        }
        execution = StageExecution(
            envelope=envelope,
            files=accepted_files,
            outputs=output_sources,
        )
        _publish_execution(attempt_root, execution)
        return execution  # noqa: TRY300
    except ExternalCancellation:
        if process is not None and process.poll() is None:
            _terminate_process_group(process, process.pid, immediate=True)
        raise
    finally:
        shutil.rmtree(work_root, ignore_errors=True)
