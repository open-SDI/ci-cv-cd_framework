"""Black-box checks that bundle validation rejects a tampered result bundle."""

from __future__ import annotations

import hashlib
import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from tests._fixture_inputs import (
    RED_RUN_EXIT,
    commit_fixture_repository,
    dispatch_local_cli,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_bundle_validation_rejects_tampered_artifact_bytes(tmp_path: Path) -> None:
    repository, commit_sha = commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = dispatch_local_cli(repository, commit_sha, bundle_root)
    assert dispatched.returncode == RED_RUN_EXIT, dispatched.stderr
    result = json.loads(dispatched.stdout)
    artifact_path = bundle_root / result["artifacts"][0]["path"]
    artifact_path.write_bytes(b"tampered\n")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert validated.stdout == ""
    assert "artifact" in validated.stderr.lower()


@pytest.mark.parametrize("mutation", ["missing", "extra", "unsafe"])
def test_bundle_validation_rejects_invalid_archive_inventory(
    tmp_path: Path, mutation: str
) -> None:
    repository, commit_sha = commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = dispatch_local_cli(repository, commit_sha, bundle_root)
    assert dispatched.returncode == RED_RUN_EXIT, dispatched.stderr
    result = json.loads(dispatched.stdout)
    artifact_path = bundle_root / result["artifacts"][0]["path"]
    if mutation == "missing":
        artifact_path.unlink()
    elif mutation == "extra":
        (bundle_root / "extra.txt").write_text("undeclared archive file")
    else:
        artifact_path.unlink()
        artifact_path.symlink_to("/dev/null")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert validated.stdout == ""


def test_bundle_validation_rejects_unbounded_artifact_grants(tmp_path: Path) -> None:
    repository, commit_sha = commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = dispatch_local_cli(repository, commit_sha, bundle_root)
    assert dispatched.returncode == RED_RUN_EXIT, dispatched.stderr
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text())
    result["attempts"][0]["accepted_files"][0]["byte_size"] = 32769
    result["artifacts"][0]["byte_size"] = 32769
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert "less than or equal to 32768" in validated.stderr


def test_bundle_validation_rejects_machine_specific_provenance_paths(
    tmp_path: Path,
) -> None:
    repository, commit_sha = commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = dispatch_local_cli(repository, commit_sha, bundle_root)
    assert dispatched.returncode == RED_RUN_EXIT, dispatched.stderr
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text())
    original = result["input_provenance"][0]["path"]
    machine_path = "/home/acceptance-user/request.yaml"
    result["input_provenance"][0]["path"] = machine_path
    for attempt in result["attempts"]:
        for accepted_input in attempt["accepted_inputs"]:
            if accepted_input["source_path"] == original:
                accepted_input["source_path"] = machine_path
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert validated.stdout == ""
    assert "repository-relative" in validated.stderr


def test_bundle_validation_rejects_reordered_or_unknown_input_provenance(
    tmp_path: Path,
) -> None:
    repository, commit_sha = commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = dispatch_local_cli(repository, commit_sha, bundle_root)
    assert dispatched.returncode == RED_RUN_EXIT, dispatched.stderr
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text())
    provenance = result["input_provenance"]
    unknown = [{**provenance[0], "schema_version": "sdi.unknown-input/v1"}]

    for variant in ([*provenance[1:], provenance[0]], [*unknown, *provenance[1:]]):
        result_path.write_text(
            json.dumps(result | {"input_provenance": variant}), encoding="utf-8"
        )
        validated = subprocess.run(
            ["sdi-integration", "validate-bundle", "--bundle-root", str(bundle_root)],
            check=False,
            capture_output=True,
            text=True,
        )

        assert validated.returncode == 2
        assert "not the identified input set" in validated.stderr


@pytest.mark.parametrize(
    "repository_identity",
    ["jenkins.internal/repository", "buildhost/repository"],
)
def test_bundle_validation_rejects_a_private_repository_identity(
    tmp_path: Path, repository_identity: str
) -> None:
    repository, commit_sha = commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = dispatch_local_cli(repository, commit_sha, bundle_root)
    assert dispatched.returncode == RED_RUN_EXIT, dispatched.stderr
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["repository"] = repository_identity
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert "repository" in validated.stderr


def test_bundle_validation_rejects_sensitive_domain_content(tmp_path: Path) -> None:
    repository, commit_sha = commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = dispatch_local_cli(repository, commit_sha, bundle_root)
    assert dispatched.returncode == RED_RUN_EXIT, dispatched.stderr
    artifact_path = bundle_root / "stages/composition/composition-blueprint.json"
    document = json.loads(artifact_path.read_text(encoding="utf-8"))
    document["services"][0]["capability"] = "jenkins.internal"
    content = (
        json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    artifact_path.write_bytes(content)
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    for artifact in result["artifacts"]:
        if artifact["path"] == "stages/composition/composition-blueprint.json":
            artifact["byte_size"] = len(content)
            artifact["sha256"] = hashlib.sha256(content).hexdigest()
    for attempt in result["attempts"]:
        for accepted in attempt["accepted_files"]:
            if accepted.get("slot") == "composition_blueprint":
                accepted["byte_size"] = len(content)
                accepted["sha256"] = hashlib.sha256(content).hexdigest()
        for accepted in attempt["accepted_inputs"]:
            if accepted["slot"] == "composition_blueprint":
                accepted["byte_size"] = len(content)
                accepted["sha256"] = hashlib.sha256(content).hexdigest()
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert "sensitive material" in validated.stderr


def test_bundle_validation_rejects_a_stage_output_under_the_wrong_schema(
    tmp_path: Path,
) -> None:
    repository, commit_sha = commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = dispatch_local_cli(repository, commit_sha, bundle_root)
    assert dispatched.returncode == RED_RUN_EXIT, dispatched.stderr
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text())
    result["attempts"][1]["accepted_files"][0]["schema_version"] = (
        "sdi.deployment-result/v1"
    )
    result["artifacts"][3]["schema_version"] = "sdi.deployment-result/v1"
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert "reviewed profile" in validated.stderr


def test_bundle_validation_rechecks_cross_stage_domain_correlation(
    tmp_path: Path,
) -> None:
    repository, commit_sha = commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = dispatch_local_cli(repository, commit_sha, bundle_root)
    assert dispatched.returncode == RED_RUN_EXIT, dispatched.stderr
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text())
    artifact = next(
        item for item in result["artifacts"] if item["slot"] == "image_build_result"
    )
    domain_path = bundle_root / artifact["path"]
    domain_output = json.loads(domain_path.read_text())
    domain_output["profile_id"] = "another-profile"
    changed_bytes = json.dumps(domain_output).encode()
    domain_path.write_bytes(changed_bytes)
    changed_sha256 = hashlib.sha256(changed_bytes).hexdigest()
    artifact["byte_size"] = len(changed_bytes)
    artifact["sha256"] = changed_sha256
    accepted = result["attempts"][1]["accepted_files"][0]
    accepted["byte_size"] = len(changed_bytes)
    accepted["sha256"] = changed_sha256
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert "correlation" in validated.stderr.lower()


def test_bundle_validation_rejects_work_after_a_negative_domain_outcome(
    tmp_path: Path,
) -> None:
    repository, commit_sha = commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = dispatch_local_cli(repository, commit_sha, bundle_root)
    assert dispatched.returncode == RED_RUN_EXIT, dispatched.stderr
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text())
    first = result["attempts"][0]
    first["domain_outcome"] = "failed"
    first["reason"] = {
        "category": "domain",
        "code": "sdi.domain.requirement-unsatisfied",
        "summary": "The represented Domain requirement was not satisfied.",
    }
    first["accepted_files"] = [
        item for item in first["accepted_files"] if item["role"] != "domain_output"
    ]
    for artifact in result["artifacts"]:
        if artifact["stage"] == "composition" and artifact["role"] == "domain_output":
            (bundle_root / artifact["path"]).unlink()
    result["artifacts"] = [
        item
        for item in result["artifacts"]
        if not (item["stage"] == "composition" and item["role"] == "domain_output")
    ]
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert "must be skipped" in validated.stderr


def test_bundle_validation_rejects_ascii_delete_in_diagnostics(tmp_path: Path) -> None:
    repository, commit_sha = commit_fixture_repository(tmp_path)
    bundle_root = tmp_path / "bundle"
    dispatched = dispatch_local_cli(repository, commit_sha, bundle_root)
    assert dispatched.returncode == RED_RUN_EXIT, dispatched.stderr
    result_path = bundle_root / "pipeline-integration-result.json"
    result = json.loads(result_path.read_text())
    artifact = next(
        item
        for item in result["artifacts"]
        if item["stage"] == "composition" and item["role"] == "diagnostic"
    )
    diagnostic_path = bundle_root / artifact["path"]
    changed_bytes = diagnostic_path.read_bytes() + b"\x7f"
    diagnostic_path.write_bytes(changed_bytes)
    changed_sha256 = hashlib.sha256(changed_bytes).hexdigest()
    artifact["byte_size"] = len(changed_bytes)
    artifact["sha256"] = changed_sha256
    accepted = result["attempts"][0]["accepted_files"][2]
    accepted["byte_size"] = len(changed_bytes)
    accepted["sha256"] = changed_sha256
    result_path.write_text(json.dumps(result), encoding="utf-8")

    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(bundle_root),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 2
    assert "sanitized" in validated.stderr.lower()
