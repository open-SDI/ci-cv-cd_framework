"""Black-box checks for the language-neutral Stage-adapter process seam."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[2]
COMMITTED_FILES = (
    "runs/s-04/s-04-tc-03-c-01-fixture.yaml",
    "requirements/s-04/deliver-book-to-joe.yaml",
    "profiles/s-04/waffle-native-arm64.yaml",
    "integration/stage-profiles/composition-v1.yaml",
    "deployment/jenkins/adapters/composition-fixture-v1.yaml",
)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit_fixture_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Test Operator")
    _git(repository, "config", "user.email", "operator@example.test")
    _git(
        repository,
        "remote",
        "add",
        "origin",
        "https://github.com/example/sdi-fixture.git",
    )
    for relative_path in COMMITTED_FILES:
        destination = repository / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((SOURCE_ROOT / relative_path).read_bytes())
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Add composition Fixture")
    return repository, _git(repository, "rev-parse", "HEAD")


def _execute(
    repository: Path,
    commit_sha: str,
    attempt_root: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sdi-integration",
            "execute-stage",
            "--repository",
            str(repository),
            "--requested-ref",
            "refs/heads/main",
            "--resolved-commit",
            commit_sha,
            "--run-request-path",
            "runs/s-04/s-04-tc-03-c-01-fixture.yaml",
            "--descriptor-path",
            "deployment/jenkins/adapters/composition-fixture-v1.yaml",
            "--attempt-root",
            str(attempt_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_executes_the_committed_composition_fixture_deterministically(
    tmp_path: Path,
) -> None:
    repository, commit_sha = _commit_fixture_repository(tmp_path)
    first_root = tmp_path / "accepted-first"
    second_root = tmp_path / "accepted-second"

    first = _execute(repository, commit_sha, first_root)
    second = _execute(repository, commit_sha, second_root)

    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    first_envelope = json.loads(first.stdout)
    second_envelope = json.loads(second.stdout)
    assert (
        first_envelope["correlation"]["execution_id"]
        != second_envelope["correlation"]["execution_id"]
    )
    assert first_envelope["lifecycle_state"] == "completed"
    assert first_envelope["execution_conclusion"] == "succeeded"
    assert first_envelope["implementation_mode"] == "fixture"
    assert first_envelope["domain_outcome"] == "not_evaluated"
    assert first_envelope["process"]["exit_code"] == 0
    assert [item["slot"] for item in first_envelope["accepted_files"]] == [
        "composition_blueprint",
        "deployment_schema",
        "diagnostic",
    ]

    for relative_path in (
        "outputs/composition-blueprint.json",
        "outputs/deployment-schema.json",
        "diagnostic.txt",
    ):
        assert (first_root / relative_path).read_bytes() == (
            second_root / relative_path
        ).read_bytes()
    assert json.loads((first_root / "accepted-attempt.json").read_text()) == (
        first_envelope
    )


def test_records_an_undeclared_candidate_without_salvaging_files(
    tmp_path: Path,
) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    profile_path = repository / "integration/stage-profiles/composition-v1.yaml"
    profile_digest = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    adapter = tmp_path / "nonconforming-adapter"
    adapter.write_text(
        """#!/usr/bin/env python3
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
(output / "undeclared.txt").write_text("must not be accepted")
response = {
    "schema_version": "sdi.stage-adapter-response/v1",
    "correlation": request["correlation"],
    "execution_conclusion": "succeeded",
    "domain_outcome": "not_evaluated",
    "reason": {
        "category": "fixture",
        "code": "sdi.fixture.nonconforming",
        "summary": "A deliberately invalid conformance candidate.",
    },
    "consumed_inputs": [item["slot"] for item in request["inputs"]],
    "produced_outputs": [],
    "diagnostic": {"present": False, "truncated": False},
}
(output / "response.json").write_text(json.dumps(response))
""",
        encoding="utf-8",
    )
    adapter.chmod(0o755)
    descriptor_path = (
        repository / "deployment/jenkins/adapters/composition-fixture-v1.yaml"
    )
    descriptor_path.write_text(
        f"""schema_version: sdi.adapter-descriptor/v1
stage: composition
implementation_mode: fixture
image: ghcr.io/gurkhaman/sdi-stage-fixture@sha256:{"1" * 64}
entrypoint: {adapter}
process_contract_version: sdi.stage-adapter-process/v1
stage_profile: integration/stage-profiles/composition-v1.yaml
stage_profile_version: sdi.composition-stage-profile/v1
stage_profile_sha256: {profile_digest}
agent_label: composition
secret_bindings: []
""",
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Select conformance adapter")
    commit_sha = _git(repository, "rev-parse", "HEAD")
    attempt_root = tmp_path / "must-not-exist"

    completed = _execute(repository, commit_sha, attempt_root)

    assert completed.returncode == 0, completed.stderr
    envelope = json.loads(completed.stdout)
    assert envelope["execution_conclusion"] == "failed"
    assert envelope["adapter_response_accepted"] is False
    assert envelope["reason"]["code"] == "sdi.adapter.undeclared-output"
    assert envelope["accepted_files"] == []
    assert {path.name for path in attempt_root.iterdir()} == {"accepted-attempt.json"}
    assert not any(
        path.read_bytes() == b"must not be accepted"
        for path in tmp_path.rglob("undeclared.txt")
        if path.is_file()
    )


def test_records_a_replaced_candidate_root_without_accepting_its_response(
    tmp_path: Path,
) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    profile_path = repository / "integration/stage-profiles/composition-v1.yaml"
    profile_digest = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    adapter = tmp_path / "root-replacing-adapter"
    adapter.write_text(
        """#!/usr/bin/env python3
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
output.rmdir()
replacement = output.parent / "replacement"
replacement.mkdir()
output.symlink_to(replacement, target_is_directory=True)
response = {
    "schema_version": "sdi.stage-adapter-response/v1",
    "correlation": request["correlation"],
    "execution_conclusion": "succeeded",
    "domain_outcome": "not_evaluated",
    "reason": {
        "category": "fixture",
        "code": "sdi.fixture.nonconforming",
        "summary": "The candidate root was replaced.",
    },
    "consumed_inputs": [item["slot"] for item in request["inputs"]],
    "produced_outputs": [],
    "diagnostic": {"present": False, "truncated": False},
}
(replacement / "response.json").write_text(json.dumps(response))
""",
        encoding="utf-8",
    )
    adapter.chmod(0o755)
    descriptor_path = (
        repository / "deployment/jenkins/adapters/composition-fixture-v1.yaml"
    )
    descriptor_path.write_text(
        f"""schema_version: sdi.adapter-descriptor/v1
stage: composition
implementation_mode: fixture
image: ghcr.io/gurkhaman/sdi-stage-fixture@sha256:{"1" * 64}
entrypoint: {adapter}
process_contract_version: sdi.stage-adapter-process/v1
stage_profile: integration/stage-profiles/composition-v1.yaml
stage_profile_version: sdi.composition-stage-profile/v1
stage_profile_sha256: {profile_digest}
agent_label: composition
secret_bindings: []
""",
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Select root-replacing adapter")
    commit_sha = _git(repository, "rev-parse", "HEAD")
    attempt_root = tmp_path / "must-not-exist"

    completed = _execute(repository, commit_sha, attempt_root)

    assert completed.returncode == 0, completed.stderr
    envelope = json.loads(completed.stdout)
    assert envelope["execution_conclusion"] == "failed"
    assert envelope["adapter_response_accepted"] is False
    assert envelope["reason"]["code"] == "sdi.adapter.response-race"
    assert envelope["accepted_files"] == []
    assert {path.name for path in attempt_root.iterdir()} == {"accepted-attempt.json"}


@pytest.mark.parametrize(
    "profile_path",
    ["/absolute/output.json", "../traversing/output.json"],
)
def test_rejects_unconfined_output_grants_before_invocation(
    tmp_path: Path, profile_path: str
) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    profile = repository / "integration/stage-profiles/composition-v1.yaml"
    profile.write_text(
        profile.read_text().replace("outputs/composition-blueprint.json", profile_path),
        encoding="utf-8",
    )
    descriptor = repository / "deployment/jenkins/adapters/composition-fixture-v1.yaml"
    descriptor.write_text(
        descriptor.read_text().replace(
            next(
                line.split(": ", 1)[1]
                for line in descriptor.read_text().splitlines()
                if line.startswith("stage_profile_sha256:")
            ),
            hashlib.sha256(profile.read_bytes()).hexdigest(),
        ),
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Add invalid output grant")
    commit_sha = _git(repository, "rev-parse", "HEAD")

    completed = _execute(repository, commit_sha, tmp_path / "must-not-exist")

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "path" in completed.stderr.lower()


def test_rejects_a_descriptor_profile_digest_mismatch_before_invocation(
    tmp_path: Path,
) -> None:
    repository, _ = _commit_fixture_repository(tmp_path)
    descriptor = repository / "deployment/jenkins/adapters/composition-fixture-v1.yaml"
    old_digest = next(
        line.split(": ", 1)[1]
        for line in descriptor.read_text().splitlines()
        if line.startswith("stage_profile_sha256:")
    )
    descriptor.write_text(
        descriptor.read_text().replace(old_digest, "a" * 64), encoding="utf-8"
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Add mismatched descriptor digest")
    commit_sha = _git(repository, "rev-parse", "HEAD")
    attempt_root = tmp_path / "must-not-exist"

    completed = _execute(repository, commit_sha, attempt_root)

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "descriptor digest" in completed.stderr
    assert not attempt_root.exists()
