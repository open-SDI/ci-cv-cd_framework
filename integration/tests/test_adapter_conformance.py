"""Every adapter refuses a bad request before it runs or publishes anything."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel

from sdi_pipeline_integration._adapter_kit import (
    DomainAdapter,
    Invocation,
    Outcome,
    Verdict,
)
from sdi_pipeline_integration._json_input import canonical_json
from sdi_pipeline_integration._stage_contracts import AdapterDescriptor, StageProfile
from sdi_pipeline_integration._stage_profiles import stage_profile_bytes
from sdi_pipeline_integration._stage_registry import STAGE_ORDER
from sdi_pipeline_integration._yaml_input import parse_yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sdi_pipeline_integration._stage_registry import StageName

REPOSITORY_ROOT = Path(__file__).parents[2]
ADAPTER_SCRIPTS = sorted(
    {
        AdapterDescriptor.model_validate(
            parse_yaml(path.read_bytes(), str(path))
        ).entrypoint
        for path in (REPOSITORY_ROOT / "deployment/jenkins/adapters").glob("*.yaml")
    }
)
INPUT_CONTENT = b"x"
REQUEST_LIMIT_BYTES = 128 * 1024

type Main = Callable[[Sequence[str] | None], int]


class BodyRanError(Exception):
    """The Domain body ran, so the adapter accepted the request."""


class _Document(BaseModel):
    value: int


def _never(_invocation: Invocation) -> Verdict:
    raise BodyRanError


def _main(script: str) -> Main:
    (entry_point,) = entry_points(group="console_scripts", name=script)
    return entry_point.load()


def _probed(script: str) -> tuple[Main, StageName]:
    main = _main(script)
    if isinstance(main, DomainAdapter):
        return replace(main, body=_never), main.stage
    return main, STAGE_ORDER[0]


def _profile(stage: StageName) -> StageProfile:
    return StageProfile.model_validate(
        parse_yaml(stage_profile_bytes(stage), f"{stage} profile")
    )


def _request(stage: StageName) -> bytes:
    profile = _profile(stage)
    return canonical_json(
        {
            "schema_version": "sdi.stage-adapter-request/v1",
            "correlation": {
                "execution_id": "0b6f5d1e-2c3a-4d5e-8f60-718293a4b5c6",
                "stage": stage,
                "attempt_number": 1,
            },
            "work_limit_seconds": profile.work_limit_seconds,
            "inputs": [
                grant.model_dump(mode="json")
                | {
                    "byte_size": len(INPUT_CONTENT),
                    "sha256": hashlib.sha256(INPUT_CONTENT).hexdigest(),
                }
                for grant in profile.inputs
            ],
            "outputs": [item.model_dump(mode="json") for item in profile.outputs],
            "diagnostic": profile.diagnostic.model_dump(mode="json"),
        }
    )


def _run(main: Main, stage: StageName, request: bytes, tmp_path: Path) -> Path:
    request_path = tmp_path / "request.json"
    request_path.write_bytes(request)
    input_root = tmp_path / "inputs"
    for grant in _profile(stage).inputs:
        (input_root / grant.path).parent.mkdir(parents=True, exist_ok=True)
        (input_root / grant.path).write_bytes(INPUT_CONTENT)
    input_root.mkdir(exist_ok=True)
    output_root = tmp_path / "candidate"
    output_root.mkdir()
    exit_code = main(
        [
            "run",
            "--request",
            str(request_path),
            "--input-root",
            str(input_root),
            "--output-root",
            str(output_root),
        ]
    )
    assert exit_code == 2, "the adapter did not refuse"
    return output_root


def _refuses(
    script: str,
    stage: StageName,
    request: bytes,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    main, _ = _probed(script)
    output_root = _run(main, stage, request, tmp_path)
    assert list(output_root.iterdir()) == [], f"{script} published before refusing"
    assert capsys.readouterr().err.startswith(f"{script}: ")


@pytest.mark.parametrize("script", ADAPTER_SCRIPTS)
def test_a_valid_request_reaches_the_domain_body(script: str, tmp_path: Path) -> None:
    main, stage = _probed(script)
    if not isinstance(main, DomainAdapter):
        pytest.skip("the Fixture adapter has no Domain body")
    with pytest.raises(BodyRanError):
        _run(main, stage, _request(stage), tmp_path)


@pytest.mark.parametrize("script", ADAPTER_SCRIPTS)
def test_an_oversized_request_is_refused(
    script: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, stage = _probed(script)
    request = _request(stage)
    padded = request + b" " * (REQUEST_LIMIT_BYTES + 1 - len(request))
    _refuses(script, stage, padded, tmp_path, capsys)


@pytest.mark.parametrize("script", ADAPTER_SCRIPTS)
def test_a_malformed_request_is_refused(
    script: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, stage = _probed(script)
    _refuses(script, stage, b"{malformed", tmp_path, capsys)


@pytest.mark.parametrize("script", ADAPTER_SCRIPTS)
def test_a_request_for_another_stage_is_refused(
    script: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main, stage = _probed(script)
    if not isinstance(main, DomainAdapter):
        pytest.skip("the Fixture adapter replays a case for every Stage")
    other = STAGE_ORDER[(STAGE_ORDER.index(stage) + 1) % len(STAGE_ORDER)]
    _refuses(script, other, _request(other), tmp_path, capsys)


@pytest.mark.parametrize("script", ADAPTER_SCRIPTS)
def test_a_verdict_naming_an_undeclared_output_publishes_nothing(
    script: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main = _main(script)
    if not isinstance(main, DomainAdapter):
        pytest.skip("the Fixture adapter publishes no Domain verdict")
    declared = _profile(main.stage).outputs[0].slot
    outcome = Outcome("done", "succeeded", "succeeded", "domain", "Done.")

    def body(_invocation: Invocation) -> Verdict:
        documents = {declared: _Document(value=1), "undeclared": _Document(value=2)}
        return Verdict(outcome, documents)

    output_root = _run(
        replace(main, body=body),
        main.stage,
        _request(main.stage),
        tmp_path,
    )
    assert list(output_root.iterdir()) == []
    assert capsys.readouterr().err == f"{script}: InputError\n"
