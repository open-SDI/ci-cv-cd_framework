"""The process shell every Domain adapter shares. Copy _image_build_adapter.py."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ValidationError

from ._jenkins_agent_boundary import enforce_domain_execution_boundary
from ._json_input import canonical_json, parse_json
from ._stage_contracts import (
    AdapterExecutionConclusion,
    AdapterRequest,
    AdapterResponse,
    DomainOutcome,
    ReasonCategory,
    contains_sensitive_material,
)
from ._stage_registry import STAGES
from ._yaml_input import InputError, validate_contract

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from ._stage_registry import StageName

MAX_REQUEST_BYTES = 128 * 1024
# Model names may carry an organization prefix, as in Qwen/Qwen3.8-27B.
DIAGNOSTIC_FACT = re.compile(r"^[A-Za-z0-9_./-]{1,128}$")
UNRECORDED = "unrecorded"


@dataclass(frozen=True)
class Outcome:
    code: str
    conclusion: AdapterExecutionConclusion
    domain_outcome: DomainOutcome
    category: ReasonCategory
    summary: str


@dataclass(frozen=True)
class Succeeded:
    code: Literal["succeeded"] = "succeeded"
    conclusion: Literal["succeeded"] = "succeeded"
    domain_outcome: Literal["succeeded"] = "succeeded"


SUCCEEDED = Succeeded()


@dataclass(frozen=True)
class Verdict:
    outcome: Outcome | Succeeded
    documents: Mapping[str, BaseModel] = field(default_factory=dict[str, BaseModel])


class Diagnostic:
    def __init__(self, prefix: str, max_bytes: int) -> None:
        self._prefix = prefix
        self._max_bytes = max_bytes
        self._facts: dict[str, str] = {}
        self._lines: list[str] = []
        self._blocks: list[str] = []
        self._cut = False

    def fact(self, name: str, value: str | int) -> None:
        text = str(value)
        if DIAGNOSTIC_FACT.fullmatch(text) is None or contains_sensitive_material(text):
            text = UNRECORDED
        self._facts[name] = text

    def facts(self, values: Mapping[str, object]) -> None:
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                self._facts.pop(name, None)
            else:
                self.fact(name, value)

    def line(self, text: str) -> None:
        self._lines.append(UNRECORDED if contains_sensitive_material(text) else text)

    def block(self, title: str, lines: Sequence[str], *, cut: bool) -> None:
        self._blocks.append(
            _block_title(title) + "".join(f"{line}\n" for line in lines)
        )
        self._cut = self._cut or cut

    def mark_cut(self) -> None:
        self._cut = True

    def room(self, outcome: Outcome | Succeeded) -> int:
        return self._max_bytes - len(self._header(outcome))

    def block_room(
        self, outcome: Outcome | Succeeded, title: str, *, shares: int
    ) -> int:
        return self.room(outcome) // shares - block_cost(title, ())

    def render(self, outcome: Outcome | Succeeded) -> tuple[bytes, bool]:
        header = self._header(outcome)
        if contains_sensitive_material(header.decode()):
            return self._cut_to_budget(self._outcome_line(outcome)), True
        if len(header) > self._max_bytes:
            return self._cut_to_budget(header), True
        content = header + "".join(self._blocks).encode()
        if len(content) > self._max_bytes or contains_sensitive_material(
            content.decode()
        ):
            return header, bool(self._blocks)
        return content, self._cut

    def _outcome_line(self, outcome: Outcome | Succeeded) -> bytes:
        return f"outcome: {self._prefix}.{outcome.code}\n".encode()

    def _header(self, outcome: Outcome | Succeeded) -> bytes:
        lines = [
            *(f"{name}: {text}" for name, text in self._facts.items()),
            *self._lines,
        ]
        return (
            self._outcome_line(outcome)
            + "".join(f"{line}\n" for line in lines).encode()
        )

    def _cut_to_budget(self, header: bytes) -> bytes:
        whole_lines = header[: header.rfind(b"\n", 0, self._max_bytes) + 1]
        return whole_lines or header[: self._max_bytes]


def _block_title(title: str) -> str:
    return f"--- {title}\n"


def block_cost(title: str, lines: Sequence[str]) -> int:
    return len(_block_title(title).encode()) + sum(
        len(line.encode()) + 1 for line in lines
    )


@dataclass(frozen=True)
class Invocation:
    request: AdapterRequest
    input_root: Path
    inputs: Mapping[str, bytes]
    deadline_monotonic: float
    diagnostic: Diagnostic


@dataclass(frozen=True)
class DomainAdapter:
    stage: StageName
    body: Callable[[Invocation], Verdict]

    def __call__(self, argv: Sequence[str] | None = None) -> int:
        started = time.monotonic()
        slug = self.stage.replace("_", "-")
        try:
            enforce_domain_execution_boundary(
                expected_label=STAGES[self.stage].agent.label
            )
            arguments = _parser(f"sdi-{slug}-adapter").parse_args(argv)
            output_root: Path = arguments.output_root
            request = load_request(
                arguments.request,
                stage=self.stage,
                input_root=arguments.input_root,
                output_root=output_root,
            )
            invocation = Invocation(
                request,
                arguments.input_root,
                _read_inputs(request, arguments.input_root),
                started + request.work_limit_seconds,
                Diagnostic(f"sdi.{slug}", request.diagnostic.max_bytes),
            )
            _publish(output_root, invocation, self.body(invocation))
        except (InputError, OSError, ValidationError) as error:
            sys.stderr.write(f"sdi-{slug}-adapter: {type(error).__name__}\n")
            return 2
        return 0


def serve(stage: StageName, body: Callable[[Invocation], Verdict]) -> DomainAdapter:
    return DomainAdapter(stage, body)


def encode_document(document: BaseModel) -> bytes:
    return canonical_json(document.model_dump(mode="json", exclude_none=True))


def load_request(
    path: Path, *, stage: StageName | None, input_root: Path, output_root: Path
) -> AdapterRequest:
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_REQUEST_BYTES + 1)
    except OSError as error:
        msg = "adapter request cannot be read"
        raise InputError(msg) from error
    request = validate_contract(
        AdapterRequest,
        parse_json(raw, str(path), max_bytes=MAX_REQUEST_BYTES),
        str(path),
    )
    if stage is not None and request.correlation.stage != stage:
        msg = f"the {stage} adapter serves only the {stage} Stage"
        raise InputError(msg)
    if (
        not input_root.is_dir()
        or not output_root.is_dir()
        or any(output_root.iterdir())
    ):
        msg = "adapter roots must exist and the output root must be empty"
        raise InputError(msg)
    return request


def atomic_write(root: Path, relative_path: str, content: bytes) -> None:
    destination = root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(destination)


def _parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--request", type=Path, required=True)
    run.add_argument("--input-root", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    return parser


def _read_inputs(request: AdapterRequest, input_root: Path) -> dict[str, bytes]:
    contents: dict[str, bytes] = {}
    for item in request.inputs:
        path = input_root / item.path
        if path.is_symlink() or not path.is_file() or path.lstat().st_nlink != 1:
            msg = "adapter input is not a single regular file"
            raise InputError(msg)
        content = path.read_bytes()
        if (
            len(content) != item.byte_size
            or hashlib.sha256(content).hexdigest() != item.sha256
        ):
            msg = "adapter input bytes do not match the request"
            raise InputError(msg)
        contents[item.slot] = content
    return contents


def _publish(output_root: Path, invocation: Invocation, verdict: Verdict) -> None:
    request = invocation.request
    outcome = verdict.outcome
    outputs = {item.slot: item.path for item in request.outputs}
    if not verdict.documents.keys() <= outputs.keys():
        msg = "the adapter verdict names an output the request does not declare"
        raise InputError(msg)
    for slot, document in verdict.documents.items():
        atomic_write(output_root, outputs[slot], encode_document(document))
    diagnostic, truncated = invocation.diagnostic.render(outcome)
    atomic_write(output_root, request.diagnostic.path, diagnostic)
    response = AdapterResponse.model_validate(
        {
            "schema_version": "sdi.stage-adapter-response/v1",
            "correlation": request.correlation.model_dump(mode="json"),
            "execution_conclusion": outcome.conclusion,
            "domain_outcome": outcome.domain_outcome,
            **(
                {}
                if isinstance(outcome, Succeeded)
                else {
                    "reason": {
                        "category": outcome.category,
                        "code": f"sdi.{request.correlation.stage.replace('_', '-')}."
                        f"{outcome.code}",
                        "summary": outcome.summary,
                    }
                }
            ),
            "consumed_inputs": [item.slot for item in request.inputs],
            "produced_outputs": list(verdict.documents),
            "diagnostic": {"present": True, "truncated": truncated},
        },
        strict=True,
        extra="forbid",
    )
    atomic_write(
        output_root,
        "response.json",
        canonical_json(response.model_dump(mode="json", exclude_none=True)),
    )
