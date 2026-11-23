"""Inputs that select the deterministic Fixture composition in test repositories.

The committed composition descriptor selects the implemented adapter. Tests of
the runtime and the Stage chain replace it with a Fixture descriptor, and supply
the service repository and composition config that the Fixture cases match by
exact digest.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]

COMPOSITION_DESCRIPTOR = "deployment/jenkins/adapters/composition-v1.yaml"
FIXTURE_COMPOSITION_CONFIG = "integration/tests/data/fixture-composition-config.yaml"
FIXTURE_SERVICE_REPOSITORY = (
    REPOSITORY_ROOT / "integration/tests/data/fixture-service-repository"
)
RUNTIME_IMAGE = (
    "docker.io/library/python@sha256:"
    "4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2"
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
    profile = repository / "integration/stage-profiles/composition-v1.yaml"
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
stage_profile_sha256: {hashlib.sha256(profile.read_bytes()).hexdigest()}
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
