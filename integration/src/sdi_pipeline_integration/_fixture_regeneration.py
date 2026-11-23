"""Regenerate the committed Fixture cases' digests and check their freshness.

Fixture cases select by exact input digests, and their payloads name the
digests of the inputs they were derived from, so one input change cascades
through all four Stages. This development command recomputes every digest,
size, and payload `source_inputs` entry from the working tree, in Stage order,
through the same input encoding the runtime uses. Everything else in a case or
payload stays hand-authored.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ruamel.yaml import YAML

from ._contracts import (
    MobilityRequirementsSpecification,
    RunRequest,
    TargetExecutionProfile,
)
from ._json_input import parse_json
from ._stage_contracts import (
    FixtureCase,
    FixtureInputMatch,
    FixtureOutput,
)
from ._stage_registry import EXPECTED_STAGE_INPUT_SLOTS
from ._stage_runtime import (
    OUTPUT_MODELS,
    composition_settings_source,
    service_repository_source,
)
from ._yaml_input import (
    MAX_INPUT_BYTES,
    InputError,
    parse_front_matter,
    parse_yaml,
    validate_contract,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

# Tests supply these same inputs, so the cases match them by exact digest.
# The config path is part of the settings digest.
FIXTURE_COMPOSITION_CONFIG = "integration/tests/data/fixture-composition-config.yaml"
FIXTURE_SERVICE_REPOSITORY = "integration/tests/data/fixture-service-repository"
CASES_ROOT = "integration/fixtures/cases"

C05_RUN_REQUEST = "runs/s-04/s-04-tc-03-c-05-fixture.yaml"
CONFORMANCE_CASES = (
    "absent-response",
    "crash",
    "handled-failure",
    "malformed-response",
    "missing-output",
    "negative-domain",
    "oversize-output",
    "response-mismatch",
    "schema-mismatch",
    "timeout",
    "undeclared-output",
    "unsafe-output",
)
# Every case directory needs exactly one entry, so no case is skipped silently.
CASE_RUN_REQUESTS: dict[str, str] = {
    "composition-s-04-tc-03-c-05": C05_RUN_REQUEST,
    "image-build-s-04-tc-03-c-05": C05_RUN_REQUEST,
    "cd-s-04-tc-03-c-05": C05_RUN_REQUEST,
    **{
        f"composition-{name}": f"runs/conformance/composition-{name}.yaml"
        for name in CONFORMANCE_CASES
    },
}
STAGE_ORDER = tuple(EXPECTED_STAGE_INPUT_SLOTS)


def encode_payload(payload: object) -> bytes:
    """Encode a Fixture payload as the exact bytes its digest is computed over."""
    text = json.dumps(
        payload, allow_nan=False, ensure_ascii=True, indent=2, sort_keys=True
    )
    return f"{text}\n".encode()


def _encode_case(case: FixtureCase) -> bytes:
    yaml = YAML(typ="safe", pure=True)
    yaml.default_flow_style = False
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    # Keep the contract's field order, which is the order cases are read in.
    yaml.representer.sort_base_mapping_type_on_output = False
    stream = StringIO()
    data = case.model_dump(mode="json", exclude_unset=True)
    yaml.dump(data, stream)  # pyright: ignore[reportUnknownMemberType]
    return stream.getvalue().encode()


def _digest(content: bytes) -> tuple[int, str]:
    return len(content), hashlib.sha256(content).hexdigest()


def _run_inputs(repository: Path, run_request_path: str) -> dict[str, bytes]:
    """Read and validate one run request and the files it references."""
    request_bytes = (repository / run_request_path).read_bytes()
    request = validate_contract(
        RunRequest, parse_yaml(request_bytes, run_request_path), run_request_path
    )
    requirements_path = request.requirements_specification
    requirements_bytes = (repository / requirements_path).read_bytes()
    front_matter, _body = parse_front_matter(requirements_bytes, requirements_path)
    validate_contract(
        MobilityRequirementsSpecification, front_matter, requirements_path
    )
    profile_path = request.combination.target_profile
    profile_bytes = (repository / profile_path).read_bytes()
    validate_contract(
        TargetExecutionProfile, parse_yaml(profile_bytes, profile_path), profile_path
    )
    return {
        "run_request": request_bytes,
        "requirements_specification": requirements_bytes,
        "target_profile": profile_bytes,
    }


def _load_cases(cases_root: Path) -> list[tuple[str, FixtureCase]]:
    directories = {path.name for path in cases_root.iterdir() if path.is_dir()}
    if directories != set(CASE_RUN_REQUESTS):
        unmapped = sorted(directories - set(CASE_RUN_REQUESTS))
        missing = sorted(set(CASE_RUN_REQUESTS) - directories)
        msg = (
            "Fixture case directories and run requests differ; "
            f"unmapped={unmapped}, missing={missing}"
        )
        raise InputError(msg)
    cases: list[tuple[str, FixtureCase]] = []
    for name in sorted(directories):
        path = cases_root / name / "case.yaml"
        source = str(path.relative_to(cases_root.parent))
        case = validate_contract(
            FixtureCase, parse_yaml(path.read_bytes(), source), source
        )
        cases.append((name, case))
    return sorted(cases, key=lambda item: (STAGE_ORDER.index(item[1].stage), item[0]))


def regenerate(repository: Path) -> dict[Path, bytes]:
    """Return the expected bytes of every generator-owned Fixture case file.

    Each Stage's input digests come from the run's committed inputs, the
    supplied Fixture inputs, and the payloads regenerated for earlier Stages.
    """
    cases_root = repository / CASES_ROOT
    supplied = {
        "service_repository": service_repository_source(
            repository / FIXTURE_SERVICE_REPOSITORY
        ).content,
        "composition_settings": composition_settings_source(
            (repository / FIXTURE_COMPOSITION_CONFIG).read_bytes(),
            path=FIXTURE_COMPOSITION_CONFIG,
            generator=None,
        ).content,
    }
    run_inputs: dict[str, dict[str, bytes]] = {}
    stage_outputs: dict[str, dict[str, bytes]] = {}
    generated: dict[Path, bytes] = {}
    for name, case in _load_cases(cases_root):
        case_root = cases_root / name
        run_request = CASE_RUN_REQUESTS[name]
        if run_request not in run_inputs:
            run_inputs[run_request] = _run_inputs(repository, run_request)
        outputs = stage_outputs.setdefault(run_request, {})
        available = run_inputs[run_request] | supplied | outputs
        match_inputs: list[FixtureInputMatch] = []
        for slot in EXPECTED_STAGE_INPUT_SLOTS[case.stage]:
            if slot not in available:
                msg = f"{name}: no earlier Fixture case produces input {slot}"
                raise InputError(msg)
            byte_size, sha256 = _digest(available[slot])
            match_inputs.append(
                FixtureInputMatch(slot=slot, byte_size=byte_size, sha256=sha256)
            )
        source_inputs = [
            {"sha256": item.sha256, "slot": item.slot} for item in match_inputs
        ]

        case_outputs: list[FixtureOutput] = []
        for output in case.outputs:
            path = case_root / output.source_path
            payload = _regenerated_payload(path, source_inputs, cases_root)
            if output.slot in outputs:
                msg = f"{name}: another Fixture case already produces {output.slot}"
                raise InputError(msg)
            outputs[output.slot] = payload
            generated[path] = payload
            byte_size, sha256 = _digest(payload)
            case_outputs.append(
                output.model_copy(update={"byte_size": byte_size, "sha256": sha256})
            )
        byte_size, sha256 = _digest(
            (case_root / case.diagnostic.source_path).read_bytes()
        )
        regenerated = case.model_copy(
            update={
                "match_inputs": match_inputs,
                "outputs": case_outputs,
                "diagnostic": case.diagnostic.model_copy(
                    update={"byte_size": byte_size, "sha256": sha256}
                ),
            }
        )
        generated[case_root / "case.yaml"] = _encode_case(regenerated)
    return generated


def _regenerated_payload(
    path: Path, source_inputs: list[dict[str, str]], cases_root: Path
) -> bytes:
    source = str(path.relative_to(cases_root.parent))
    payload: dict[str, Any] = parse_json(
        path.read_bytes(), source, max_bytes=MAX_INPUT_BYTES
    )
    payload["source_inputs"] = source_inputs
    model = OUTPUT_MODELS.get(payload.get("schema_version", ""))
    if model is None:
        msg = f"{source}: Fixture payload uses an unsupported schema version"
        raise InputError(msg)
    validate_contract(model, payload, source)
    return encode_payload(payload)


def _stale_files(repository: Path) -> dict[Path, bytes]:
    return {
        path: content
        for path, content in regenerate(repository).items()
        if not path.is_file() or path.read_bytes() != content
    }


def write_fixture_cases(repository: Path) -> None:
    """Write every regenerated Fixture case file that differs from disk."""
    for path, content in _stale_files(repository).items():
        path.write_bytes(content)


def check_fixture_cases(repository: Path) -> None:
    """Reject Fixture case files whose digests, sizes, or encoding are stale."""
    stale = sorted(
        str(path.relative_to(repository)) for path in _stale_files(repository)
    )
    if stale:
        msg = f"Fixture cases are stale: {stale}"
        raise InputError(msg)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Fixture case regeneration command."""
    parser = argparse.ArgumentParser(
        prog="regenerate-fixtures",
        description="Regenerate or freshness-check the committed Fixture cases.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path(__file__).resolve().parents[3],
        help="repository root holding the cases and their inputs",
    )
    arguments = parser.parse_args(argv)
    try:
        if arguments.write:
            write_fixture_cases(arguments.repository)
        else:
            check_fixture_cases(arguments.repository)
    except (InputError, OSError) as error:
        sys.stderr.write(f"regenerate-fixtures: {error}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
