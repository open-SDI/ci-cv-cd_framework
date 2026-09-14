"""Deterministic Fixture implementation of the Stage-adapter process seam."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from ._jenkins_agent_boundary import enforce_domain_execution_boundary
from ._json_input import parse_json
from ._stage_contracts import (
    ASCII_CONTROL_LIMIT,
    ASCII_DELETE,
    AdapterRequest,
    AdapterResponse,
    FixtureCase,
)
from ._yaml_input import InputError, parse_yaml

if TYPE_CHECKING:
    from collections.abc import Sequence
    from importlib.resources.abc import Traversable

MAX_REQUEST_BYTES = 128 * 1024


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _atomic_write(root: Path, relative_path: str, content: bytes) -> None:
    destination = root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(destination)


def _load_request(path: Path) -> AdapterRequest:
    try:
        parsed = parse_json(path.read_bytes(), str(path), max_bytes=MAX_REQUEST_BYTES)
        return AdapterRequest.model_validate(parsed, strict=True, extra="forbid")
    except ValidationError as error:
        msg = f"adapter request contract validation failed: {error}"
        raise InputError(msg) from error


def _load_cases() -> list[tuple[FixtureCase, Traversable]]:
    cases: list[tuple[FixtureCase, Traversable]] = []
    fixture_root = files("sdi_pipeline_integration").joinpath("_fixture_cases")
    if not fixture_root.is_dir():
        fixture_root = files("sdi_pipeline_integration").joinpath(
            "../../fixtures/cases"
        )
    for case_root in sorted(fixture_root.iterdir(), key=lambda item: item.name):
        case_path = case_root.joinpath("case.yaml")
        if not case_path.is_file():
            continue
        try:
            parsed = parse_yaml(case_path.read_bytes(), str(case_path))
            case = FixtureCase.model_validate(parsed, strict=True, extra="forbid")
        except ValidationError as error:
            msg = f"Fixture case contract validation failed: {case_path.name}"
            raise InputError(msg) from error
        cases.append((case, case_root))
    return cases


def _input_facts(
    request: AdapterRequest, input_root: Path
) -> dict[str, tuple[int, str]]:
    facts: dict[str, tuple[int, str]] = {}
    for declared in request.inputs:
        path = input_root / declared.path
        stat_result = path.lstat()
        if not path.is_file() or path.is_symlink() or stat_result.st_nlink != 1:
            msg = "adapter input is not a single regular file"
            raise InputError(msg)
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if len(content) != declared.byte_size or digest != declared.sha256:
            msg = "adapter input bytes do not match the request"
            raise InputError(msg)
        facts[declared.slot] = (len(content), digest)
    return facts


def _select_case(
    request: AdapterRequest, input_root: Path
) -> tuple[FixtureCase, Traversable] | None:
    actual = _input_facts(request, input_root)
    matches = [
        (case, directory)
        for case, directory in _load_cases()
        if case.stage == request.correlation.stage
        and {item.slot: (item.byte_size, item.sha256) for item in case.match_inputs}
        == actual
    ]
    if len(matches) > 1:
        msg = "more than one Fixture case matches the exact input digests"
        raise InputError(msg)
    return matches[0] if matches else None


def _read_fixture_payload(
    case_root: Traversable,
    source_path: str,
    *,
    expected_size: int,
    expected_sha256: str,
    granted_size: int,
) -> bytes:
    path = case_root.joinpath(source_path)
    if not path.is_file():
        msg = "Fixture payload is not a single regular file"
        raise InputError(msg)
    content = path.read_bytes()
    if (
        len(content) != expected_size
        or hashlib.sha256(content).hexdigest() != expected_sha256
        or len(content) > granted_size
    ):
        msg = "Fixture payload bytes do not match the committed case"
        raise InputError(msg)
    return content


def _response(  # noqa: PLR0913
    request: AdapterRequest,
    *,
    conclusion: str,
    code: str,
    summary: str,
    category: str = "fixture",
    domain_outcome: str = "not_evaluated",
    produced_outputs: list[str],
    diagnostic_present: bool,
) -> bytes:
    response = AdapterResponse.model_validate(
        {
            "schema_version": "sdi.stage-adapter-response/v1",
            "correlation": request.correlation.model_dump(mode="json"),
            "execution_conclusion": conclusion,
            "domain_outcome": domain_outcome,
            "reason": {
                "category": category,
                "code": code,
                "summary": summary,
            },
            "consumed_inputs": [item.slot for item in request.inputs],
            "produced_outputs": produced_outputs,
            "diagnostic": {
                "present": diagnostic_present,
                "truncated": False,
            },
        },
        strict=True,
        extra="forbid",
    )
    return _canonical_json(response.model_dump(mode="json"))


def run_adapter(  # noqa: C901, PLR0911, PLR0912, PLR0915
    *, request_path: Path, input_root: Path, output_root: Path
) -> None:
    """Publish one complete candidate response after all candidate files."""
    enforce_domain_execution_boundary()
    request = _load_request(request_path)
    enforce_domain_execution_boundary(
        expected_label=request.correlation.stage.replace("_", "-")
    )
    if (
        not input_root.is_dir()
        or not output_root.is_dir()
        or any(output_root.iterdir())
    ):
        msg = "adapter roots must exist and the output root must be empty"
        raise InputError(msg)

    selected = _select_case(request, input_root)
    if selected is None:
        response = _response(
            request,
            conclusion="failed",
            code="sdi.fixture.input-mismatch",
            summary="No Fixture case matched the exact declared input digests.",
            produced_outputs=[],
            diagnostic_present=False,
        )
        _atomic_write(output_root, "response.json", response)
        return

    case, case_root = selected
    output_by_slot = {item.slot: item for item in request.outputs}
    if case.behavior == "absent_response":
        return
    if case.behavior == "crash":
        os._exit(70)
    if case.behavior == "timeout":
        time.sleep(request.work_limit_seconds + 60)
    if case.behavior == "malformed_response":
        _atomic_write(output_root, "response.json", b"{malformed")
        return
    if case.behavior == "undeclared_output":
        _atomic_write(output_root, "undeclared.txt", b"undeclared Fixture output\n")
        return
    if case.behavior == "unsafe_output":
        (output_root / "unsafe-link").symlink_to("/dev/null")
        return
    if case.behavior == "oversize_output":
        grant = request.outputs[0]
        _atomic_write(output_root, grant.path, b"x" * (grant.max_bytes + 1))
        return
    if case.behavior == "missing_output":
        response = _response(
            request,
            conclusion="succeeded",
            code=case.reason.code,
            summary=case.reason.summary,
            produced_outputs=[item.slot for item in request.outputs],
            diagnostic_present=False,
        )
        _atomic_write(output_root, "response.json", response)
        return
    if case.behavior == "schema_mismatch":
        for grant in request.outputs:
            _atomic_write(output_root, grant.path, b"{}\n")
        response = _response(
            request,
            conclusion="succeeded",
            code=case.reason.code,
            summary=case.reason.summary,
            produced_outputs=[item.slot for item in request.outputs],
            diagnostic_present=False,
        )
        _atomic_write(output_root, "response.json", response)
        return
    if case.behavior == "response_mismatch":
        response = json.loads(
            _response(
                request,
                conclusion="failed",
                code=case.reason.code,
                summary=case.reason.summary,
                produced_outputs=[],
                diagnostic_present=False,
            )
        )
        response["correlation"]["attempt_number"] += 1
        _atomic_write(output_root, "response.json", _canonical_json(response))
        return
    if case.execution_conclusion == "succeeded" and set(output_by_slot) != {
        item.slot for item in case.outputs
    }:
        msg = "Fixture outputs do not match the request grants"
        raise InputError(msg)

    for fixture_output in case.outputs:
        grant = output_by_slot[fixture_output.slot]
        content = _read_fixture_payload(
            case_root,
            fixture_output.source_path,
            expected_size=fixture_output.byte_size,
            expected_sha256=fixture_output.sha256,
            granted_size=grant.max_bytes,
        )
        _atomic_write(output_root, grant.path, content)

    diagnostic = _read_fixture_payload(
        case_root,
        case.diagnostic.source_path,
        expected_size=case.diagnostic.byte_size,
        expected_sha256=case.diagnostic.sha256,
        granted_size=request.diagnostic.max_bytes,
    )
    try:
        diagnostic_text = diagnostic.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        msg = "Fixture diagnostic is not valid UTF-8"
        raise InputError(msg) from error
    if "\0" in diagnostic_text or any(
        (ord(character) < ASCII_CONTROL_LIMIT and character not in "\t\r\n")
        or ord(character) == ASCII_DELETE
        for character in diagnostic_text
    ):
        msg = "Fixture diagnostic contains unsanitized control characters"
        raise InputError(msg)
    _atomic_write(output_root, request.diagnostic.path, diagnostic)
    response = _response(
        request,
        conclusion=case.execution_conclusion,
        code=case.reason.code,
        summary=case.reason.summary,
        category=case.reason.category,
        domain_outcome=case.domain_outcome,
        produced_outputs=[item.slot for item in case.outputs],
        diagnostic_present=True,
    )
    _atomic_write(output_root, "response.json", response)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdi-fixture-adapter")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--request", type=Path, required=True)
    run.add_argument("--input-root", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Expose only the stable one-shot Stage-adapter operation."""
    try:
        enforce_domain_execution_boundary()
        arguments = _parser().parse_args(argv)
        run_adapter(
            request_path=arguments.request,
            input_root=arguments.input_root,
            output_root=arguments.output_root,
        )
    except (InputError, OSError, ValidationError) as error:
        sys.stderr.write(f"sdi-fixture-adapter: {error}\n")
        return 2
    return 0
