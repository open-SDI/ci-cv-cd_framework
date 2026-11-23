"""Inputs that select the deterministic Fixture composition in test repositories.

The committed composition descriptor selects the implemented adapter. Tests of
the runtime and the Stage chain replace it with a Fixture descriptor, and supply
the service repository and composition config that the Fixture cases match by
exact digest.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from sdi_pipeline_integration import _fixture_regeneration
from sdi_pipeline_integration._integration_code import BAKED_ROOTS

REPOSITORY_ROOT = Path(__file__).parents[2]

COMPOSITION_DESCRIPTOR = "deployment/jenkins/adapters/composition-v1.yaml"
# The Fixture cases are generated from these same inputs.
FIXTURE_COMPOSITION_CONFIG = _fixture_regeneration.FIXTURE_COMPOSITION_CONFIG
FIXTURE_SERVICE_REPOSITORY = (
    REPOSITORY_ROOT / _fixture_regeneration.FIXTURE_SERVICE_REPOSITORY
)
RUNTIME_IMAGE = (
    "docker.io/library/python@sha256:"
    "229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"
)
SUPPLIED_INPUT_ARGUMENTS = (
    "--service-repository",
    str(FIXTURE_SERVICE_REPOSITORY),
    "--composition-config",
    FIXTURE_COMPOSITION_CONFIG,
)


def write_fixture_composition_descriptor(
    repository: Path,
    *,
    entrypoint: str | Path = "sdi-fixture-adapter",
    mode: str = "fixture",
) -> None:
    """Select a composition adapter bound to the repository's reviewed profile."""
    descriptor = repository / COMPOSITION_DESCRIPTOR
    descriptor.parent.mkdir(parents=True, exist_ok=True)
    descriptor.write_text(
        f"""schema_version: sdi.adapter-descriptor/v1
stage: composition
implementation_mode: {mode}
image: {RUNTIME_IMAGE}
entrypoint: {entrypoint}
process_contract_version: sdi.stage-adapter-process/v1
stage_profile: integration/stage-profiles/composition-v1.yaml
stage_profile_version: sdi.composition-stage-profile/v1
agent_label: composition
secret_bindings: []
""",
        encoding="utf-8",
    )


SCRIPTED_ADAPTER_PREAMBLE = """#!/usr/bin/env python3
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
commands = parser.add_subparsers(dest="command", required=True)
run = commands.add_parser("run")
run.add_argument("--request", required=True)
run.add_argument("--input-root", required=True)
run.add_argument("--output-root", required=True)
arguments = parser.parse_args()
request = json.loads(Path(arguments.request).read_text())
output = Path(arguments.output_root)
"""


def write_scripted_adapter(path: Path, body: str) -> None:
    """Write an executable adapter whose body sees `request` and `output`."""
    path.write_text(SCRIPTED_ADAPTER_PREAMBLE + body, encoding="utf-8")
    path.chmod(0o755)


def copy_baked_integration_code(repository: Path) -> None:
    """Copy the tracked integration code an agent image bakes into a repository."""
    tracked = subprocess.run(
        [
            "git",
            "-C",
            str(REPOSITORY_ROOT),
            "ls-files",
            "-z",
            "--",
            *(f"integration/{root}" for root in BAKED_ROOTS),
        ],
        check=True,
        capture_output=True,
    ).stdout.decode()
    for relative_path in filter(None, tracked.split("\0")):
        destination = repository / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((REPOSITORY_ROOT / relative_path).read_bytes())
