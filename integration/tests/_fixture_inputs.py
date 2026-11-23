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
from sdi_pipeline_integration._stage_registry import STAGES

REPOSITORY_ROOT = Path(__file__).parents[2]

COMPOSITION_DESCRIPTOR = "deployment/jenkins/adapters/composition-v1.yaml"
STAGE_DESCRIPTORS = tuple(
    sorted(
        {
            path
            for stage in STAGES.values()
            for path in (stage.jenkins_descriptor, stage.local_descriptor)
        }
    )
)
# The Fixture cases are generated from these same inputs.
FIXTURE_COMPOSITION_CONFIG = _fixture_regeneration.FIXTURE_COMPOSITION_CONFIG
FIXTURE_SERVICE_REPOSITORY = (
    REPOSITORY_ROOT / _fixture_regeneration.FIXTURE_SERVICE_REPOSITORY
)
RUNTIME_IMAGE = (
    "docker.io/library/python@sha256:"
    "229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"
)
RED_RUN_EXIT = 1
SUPPLIED_INPUT_ARGUMENTS = (
    "--service-repository",
    str(FIXTURE_SERVICE_REPOSITORY),
    "--composition-config",
    FIXTURE_COMPOSITION_CONFIG,
)

S04_COMBINATION_EXPECTATIONS: dict[str, dict[str, str]] = {
    "C-05": {
        "run_request_path": "runs/s-04/s-04-tc-03-c-05-fixture.yaml",
        "profile_id": "waffle-jetson-arm64",
        "host": "orin",
    },
}
S04_RUN_REQUEST_PATHS = tuple(
    expected["run_request_path"] for expected in S04_COMBINATION_EXPECTATIONS.values()
)
RUN_REQUEST_PATH = S04_RUN_REQUEST_PATHS[0]
COMMITTED_FILES = (
    *S04_RUN_REQUEST_PATHS,
    "requirements/s-04/deliver-book-to-joe.md",
    "profiles/s-04/waffle-native-arm64.yaml",
    "profiles/s-04/waffle-xycar-amd64.yaml",
    "profiles/s-04/burger-native-arm64.yaml",
    "profiles/s-04/burger-xycar-amd64.yaml",
    "profiles/s-04/waffle-jetson-arm64.yaml",
    "profiles/s-04/burger-jetson-arm64.yaml",
    "integration/stage-profiles/composition-v1.yaml",
    "integration/stage-profiles/image-build-v1.yaml",
    "integration/stage-profiles/cv-v1.yaml",
    "integration/stage-profiles/cd-v1.yaml",
    *STAGE_DESCRIPTORS,
    "runs/conformance/composition-negative-domain.yaml",
    "runs/conformance/composition-absent-response.yaml",
    "runs/conformance/composition-handled-failure.yaml",
    "runs/conformance/composition-crash.yaml",
    "runs/conformance/composition-timeout.yaml",
    "runs/conformance/composition-malformed-response.yaml",
    "runs/conformance/composition-missing-output.yaml",
    "runs/conformance/composition-undeclared-output.yaml",
    "runs/conformance/composition-unsafe-output.yaml",
    "runs/conformance/composition-oversize-output.yaml",
    "runs/conformance/composition-response-mismatch.yaml",
    "runs/conformance/composition-schema-mismatch.yaml",
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


def run_git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def commit_fixture_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    run_git(repository, "init", "--initial-branch=main")
    run_git(repository, "config", "user.name", "Test Operator")
    run_git(repository, "config", "user.email", "operator@example.test")
    run_git(
        repository,
        "remote",
        "add",
        "origin",
        "https://github.com/example/sdi-fixture.git",
    )
    for relative_path in COMMITTED_FILES:
        destination = repository / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((REPOSITORY_ROOT / relative_path).read_bytes())
    write_fixture_composition_descriptor(repository)
    run_git(repository, "add", ".")
    run_git(repository, "commit", "-m", "Add four-Stage Fixture inputs")
    return repository, run_git(repository, "rev-parse", "HEAD")


def dispatch_local_cli(
    repository: Path,
    commit_sha: str,
    bundle_root: Path,
    run_request_path: str = RUN_REQUEST_PATH,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sdi-integration",
            "dispatch-local",
            "--repository",
            str(repository),
            "--requested-ref",
            "refs/heads/main",
            "--resolved-commit",
            commit_sha,
            "--run-request-path",
            run_request_path,
            "--bundle-root",
            str(bundle_root),
            *SUPPLIED_INPUT_ARGUMENTS,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
