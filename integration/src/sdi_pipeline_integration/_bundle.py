"""Result bundle assembly, parsing, and archive validation."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from pydantic import BaseModel, ValidationError

from ._cd_deployment import manifest_yaml
from ._domain_contracts import DeploymentResult
from ._json_input import canonical_json, parse_json
from ._result_contracts import (
    FIXTURE_NOTICE,
    PIPELINE_RESULT_PATH,
    PipelineIntegrationResult,
    archive_path,
)
from ._stage_contracts import (
    ASCII_CONTROL_LIMIT,
    ASCII_DELETE,
    contains_sensitive_contract_material,
    contains_sensitive_material,
)
from ._stage_documents import validate_stage_documents
from ._stage_registry import STAGES, StageName
from ._stage_runtime import (
    OUTPUT_MODELS,
    StageExecution,
    capture_tree,
    expected_directories,
)
from ._yaml_input import InputError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

MAX_RESULT_BYTES = 1024 * 1024


def _write_file(root: Path, relative_path: str, content: bytes) -> None:
    destination = root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _read_initial_result(
    bundle_root: Path, expected_root_identity: tuple[int, int]
) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    root_descriptor = os.open(bundle_root, flags | os.O_DIRECTORY | no_follow)
    try:
        root_metadata = os.fstat(root_descriptor)
        if (root_metadata.st_dev, root_metadata.st_ino) != expected_root_identity:
            msg = "archive candidate root was replaced"
            raise InputError(msg)
        result_descriptor = os.open(
            PIPELINE_RESULT_PATH,
            flags | no_follow,
            dir_fd=root_descriptor,
        )
        try:
            metadata = os.fstat(result_descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_dev != root_metadata.st_dev
                or metadata.st_size > MAX_RESULT_BYTES
            ):
                msg = "Pipeline integration result is not a bounded regular file"
                raise InputError(msg)
            content = b""
            while len(content) <= MAX_RESULT_BYTES:
                chunk = os.read(
                    result_descriptor,
                    min(65536, MAX_RESULT_BYTES + 1 - len(content)),
                )
                if not chunk:
                    break
                content += chunk
            after = os.fstat(result_descriptor)
        finally:
            os.close(result_descriptor)
    except FileNotFoundError as error:
        msg = f"archive candidate is missing {PIPELINE_RESULT_PATH}"
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
    if len(content) != metadata.st_size or any(
        getattr(metadata, field) != getattr(after, field) for field in stable_fields
    ):
        msg = "Pipeline integration result changed while being read"
        raise InputError(msg)
    return content


def parse_pipeline_result(content: bytes) -> PipelineIntegrationResult:
    """Parse one bounded strict Pipeline integration result."""
    try:
        return PipelineIntegrationResult.model_validate(
            parse_json(content, PIPELINE_RESULT_PATH, max_bytes=MAX_RESULT_BYTES),
            strict=True,
            extra="forbid",
        )
    except ValidationError as error:
        msg = f"Pipeline integration result contract validation failed: {error}"
        raise InputError(msg) from error


def validate_bundle(bundle_root: Path) -> PipelineIntegrationResult:
    """Validate the complete archive candidate against its authoritative result."""
    return validate_bundle_documents(bundle_root).result


def deployment_manifest(bundle_root: Path) -> bytes | None:
    evidence = validate_bundle_documents(bundle_root).succeeded_stage_evidence["cd"]
    result = next(
        (item for item in evidence if isinstance(item, DeploymentResult)), None
    )
    if result is None or result.manifest is None:
        return None
    return manifest_yaml(result.manifest)


class ValidatedBundle(NamedTuple):
    result: PipelineIntegrationResult
    succeeded_domain_outputs: Mapping[str, BaseModel]
    succeeded_stage_evidence: Mapping[StageName, Sequence[BaseModel]]


def validate_bundle_documents(  # noqa: C901, PLR0912, PLR0915
    bundle_root: Path,
) -> ValidatedBundle:
    if not bundle_root.is_dir() or bundle_root.is_symlink():
        msg = "archive candidate root must be a directory"
        raise InputError(msg)
    root_metadata = bundle_root.lstat()
    root_identity = (root_metadata.st_dev, root_metadata.st_ino)
    initial_result = _read_initial_result(bundle_root, root_identity)
    result = parse_pipeline_result(initial_result)
    grants = {PIPELINE_RESULT_PATH: MAX_RESULT_BYTES}
    grants.update({artifact.path: artifact.byte_size for artifact in result.artifacts})
    paths, captured = capture_tree(bundle_root, root_identity, grants)
    expected_files = {PIPELINE_RESULT_PATH, *(item.path for item in result.artifacts)}
    if paths != expected_files | expected_directories(expected_files):
        msg = "archive candidate inventory does not match its result"
        raise InputError(msg)
    if captured[PIPELINE_RESULT_PATH] != initial_result:
        msg = "Pipeline integration result changed before archive capture"
        raise InputError(msg)
    domain_documents: dict[StageName, dict[str, BaseModel]] = {
        stage: {} for stage in STAGES
    }
    evidence_documents: dict[StageName, list[BaseModel]] = {
        stage: [] for stage in STAGES
    }
    for artifact in result.artifacts:
        content = captured[artifact.path]
        if (
            len(content) != artifact.byte_size
            or hashlib.sha256(content).hexdigest() != artifact.sha256
        ):
            msg = f"archive artifact does not match its inventory: {artifact.path}"
            raise InputError(msg)
        if artifact.role == "stage_attachment":
            continue
        if artifact.role != "diagnostic":
            model = OUTPUT_MODELS.get(artifact.schema_version)
            if model is None:
                msg = f"archive artifact uses an unsupported schema: {artifact.path}"
                raise InputError(msg)
            try:
                document = model.model_validate(
                    parse_json(content, artifact.path, max_bytes=artifact.byte_size),
                    strict=True,
                    extra="forbid",
                )
            except ValidationError as error:
                msg = f"archive artifact is invalid: {artifact.path}: {error}"
                raise InputError(msg) from error
            if artifact.role == "stage_evidence":
                evidence_documents[artifact.stage].append(document)
                continue
            if contains_sensitive_contract_material(document.model_dump(mode="json")):
                msg = (
                    "archive Domain output contains sensitive material: "
                    f"{artifact.path}"
                )
                raise InputError(msg)
            domain_documents[artifact.stage][artifact.slot] = document
        else:
            try:
                diagnostic = content.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                msg = f"archive diagnostic is not valid UTF-8: {artifact.path}"
                raise InputError(msg) from error
            if "\0" in diagnostic or any(
                (ord(character) < ASCII_CONTROL_LIMIT and character not in "\t\r\n")
                or ord(character) == ASCII_DELETE
                for character in diagnostic
            ):
                msg = f"archive diagnostic is not sanitized: {artifact.path}"
                raise InputError(msg)
            if contains_sensitive_material(diagnostic):
                msg = f"archive diagnostic contains sensitive material: {artifact.path}"
                raise InputError(msg)
    available_documents: dict[str, BaseModel] = {}
    accepted_evidence: dict[StageName, list[BaseModel]] = {
        stage: [] for stage in STAGES
    }
    identity = (
        result.scenario_id,
        result.testcase_id,
        result.combination_id,
        result.profile_id,
    )
    for attempt in result.attempts:
        stage = attempt.correlation.stage
        if attempt.execution_conclusion != "succeeded":
            continue
        stage_inputs = {
            accepted.slot: available_documents[accepted.slot]
            for accepted in attempt.accepted_inputs
            if accepted.slot in available_documents
        }
        if any(
            getattr(document, "evidence_basis", None) != attempt.implementation_mode
            for document in [
                *domain_documents[stage].values(),
                *evidence_documents[stage],
            ]
        ):
            msg = f"{stage} archive evidence basis does not match its adapter"
            raise InputError(msg)
        validate_stage_documents(
            stage,
            domain_documents[stage],
            stage_inputs,
            {item.slot: item.sha256 for item in attempt.accepted_inputs},
            identity,
            implementation_mode=attempt.implementation_mode,
            domain_outcome=attempt.domain_outcome,
            evidence=evidence_documents[stage],
        )
        available_documents.update(domain_documents[stage])
        accepted_evidence[stage] = evidence_documents[stage]
    second_paths, second_capture = capture_tree(bundle_root, root_identity, grants)
    if second_paths != paths or second_capture != captured:
        msg = "archive candidate changed after validation"
        raise InputError(msg)
    return ValidatedBundle(result, available_documents, accepted_evidence)


def assemble_bundle_staging(
    *,
    identified: dict[str, Any],
    run_started_at: datetime,
    executions: list[StageExecution],
    bundle_staging: Path,
) -> dict[str, Any]:
    artifact_records: list[dict[str, object]] = []
    for execution in executions:
        stage = execution.envelope.correlation.stage
        for accepted in execution.envelope.accepted_files:
            destination = archive_path(stage, accepted.path)
            _write_file(bundle_staging, destination, execution.files[accepted.path])
            record = cast("dict[str, object]", accepted.model_dump(mode="json"))
            record["stage"] = stage
            record["path"] = destination
            artifact_records.append(record)

    finished_at = datetime.now(UTC)
    result = PipelineIntegrationResult.model_validate(
        {
            "schema_version": "sdi.pipeline-integration-result/v1",
            "execution_id": identified["execution_id"],
            "repository": identified["repository"],
            "requested_ref": identified["requested_ref"],
            "resolved_commit_sha": identified["resolved_commit_sha"],
            "scenario_id": identified["scenario_id"],
            "testcase_id": identified["testcase_id"],
            "combination_id": identified["combination_id"],
            "profile_id": identified["profile_id"],
            "started_at": run_started_at,
            "finished_at": finished_at,
            "duration_ms": int((finished_at - run_started_at).total_seconds() * 1000),
            "input_provenance": identified["inputs"],
            "attempts": [
                item.envelope.model_dump(mode="json", exclude_none=True)
                for item in executions
            ],
            "artifacts": artifact_records,
            "fixture_notice": (
                FIXTURE_NOTICE
                if any(
                    item.envelope.implementation_mode == "fixture"
                    for item in executions
                )
                else None
            ),
            "kpi_evaluation": "not_evaluated",
        },
        strict=True,
        extra="forbid",
    )
    _write_file(
        bundle_staging,
        PIPELINE_RESULT_PATH,
        canonical_json(result.model_dump(mode="json", exclude_none=True)),
    )
    validate_bundle(bundle_staging)
    return result.model_dump(mode="json", exclude_none=True)


def assemble_bundle(
    *,
    identified: dict[str, Any],
    run_started_at: datetime,
    executions: list[StageExecution],
    bundle_root: Path,
) -> dict[str, Any]:
    """Atomically assemble accepted executions into one validated result bundle."""
    if bundle_root.exists() or bundle_root.is_symlink():
        msg = "bundle root must not already exist"
        raise InputError(msg)
    bundle_root.parent.mkdir(parents=True, exist_ok=True)
    claim_path = bundle_root.parent / f".{bundle_root.name}.claim"
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
            msg = "bundle root is already claimed by another assembly"
            raise InputError(msg) from error
        os.close(claim_descriptor)
        claim_acquired = True
        bundle_staging = Path(
            tempfile.mkdtemp(
                prefix=f".{bundle_root.name}.bundle-", dir=bundle_root.parent
            )
        )
        result = assemble_bundle_staging(
            identified=identified,
            run_started_at=run_started_at,
            executions=executions,
            bundle_staging=bundle_staging,
        )
        bundle_staging.replace(bundle_root)
        return result
    finally:
        if bundle_staging is not None and bundle_staging.exists():
            shutil.rmtree(bundle_staging, ignore_errors=True)
        if claim_acquired:
            claim_path.unlink(missing_ok=True)
