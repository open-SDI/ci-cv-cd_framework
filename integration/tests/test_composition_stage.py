"""Black-box checks for the implemented composition Stage in a local run.

The composition adapter calls a real model. These tests assert the run's
contract, never which services the model chose or how it worded them.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
from typing import TYPE_CHECKING, Any, cast

import pytest

from tests._live_model import (
    COMPOSITION_CONFIG,
    REPOSITORY_ROOT,
    lexical_only_config,
    live_generator,
    live_keys,
    openai_key,
)
from tests._proposals import COMPLETE
from tests._stub_provider import (
    BODIES,
    STUB_KEY,
    UNAVAILABLE_EMBEDDINGS,
    proposals_body,
    stub_config,
    stub_provider,
)

if TYPE_CHECKING:
    from pathlib import Path

RUN_REQUEST_PATH = "runs/s-04/s-04-tc-03-c-05-fixture.yaml"
COMMITTED_FILES = (
    RUN_REQUEST_PATH,
    "requirements/s-04/deliver-book-to-joe.md",
    "profiles/s-04/waffle-jetson-arm64.yaml",
    "integration/stage-profiles/composition-v1.yaml",
    "integration/stage-profiles/image-build-v1.yaml",
    "integration/stage-profiles/cv-v1.yaml",
    "integration/stage-profiles/cd-v1.yaml",
    "deployment/jenkins/adapters/composition-v1.yaml",
    "deployment/jenkins/adapters/image-build-fixture-v1.yaml",
    "deployment/jenkins/adapters/cv-fixture-v1.yaml",
    "deployment/jenkins/adapters/cd-fixture-v1.yaml",
    "integration/composition-config.yaml",
)
CONFIG_PATH = "integration/composition-config.yaml"
SERVICE_REPOSITORY = REPOSITORY_ROOT / "service-repository"
KEY_NAMES = ("OPENAI_API_KEY", "VLLM_KEY")
NO_RESULT_CODES = {
    "sdi.composition.bounded-no-result",
    "sdi.composition.scoped-rejection",
    "sdi.composition.insufficient-proposals",
}


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path, extra: dict[str, str] | None = None) -> str:
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
        "https://github.com/example/sdi-composition.git",
    )
    for relative_path in COMMITTED_FILES:
        destination = repository / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((REPOSITORY_ROOT / relative_path).read_bytes())
    for relative_path, content in (extra or {}).items():
        (repository / relative_path).write_text(content)
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "Add composition inputs")
    return _git(repository, "rev-parse", "HEAD")


def _dispatch(
    tmp_path: Path,
    commit_sha: str,
    *,
    generator: str | None = None,
    service_repository: Path = SERVICE_REPOSITORY,
    keys: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Dispatch a local run with the committed default composition config."""
    environment = {
        name: value for name, value in os.environ.items() if name not in KEY_NAMES
    }
    return subprocess.run(
        [
            "sdi-integration",
            "dispatch-local",
            "--repository",
            str(tmp_path / "repository"),
            "--requested-ref",
            "refs/heads/main",
            "--resolved-commit",
            commit_sha,
            "--run-request-path",
            RUN_REQUEST_PATH,
            "--service-repository",
            str(service_repository),
            *([] if generator is None else ["--generator", generator]),
            "--bundle-root",
            str(tmp_path / "bundle"),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment | (keys or {}),
    )


def _dispatch_stub(
    tmp_path: Path,
    body: dict[str, object],
    **config: Any,  # noqa: ANN401
) -> subprocess.CompletedProcess[str]:
    """Dispatch with the stub committed as the default composition config."""
    with stub_provider(body) as base_url:
        commit_sha = _repository(
            tmp_path, {CONFIG_PATH: stub_config(base_url, **config)}
        )
        return _dispatch(tmp_path, commit_sha, keys={"VLLM_KEY": STUB_KEY})


def _composition_files(result: dict[str, object]) -> set[str]:
    artifacts = cast("list[dict[str, str]]", result["artifacts"])
    return {item["slot"] for item in artifacts if item["stage"] == "composition"}


def _bundle_text(tmp_path: Path) -> str:
    return "".join(
        path.read_text()
        for path in sorted((tmp_path / "bundle").rglob("*"))
        if path.is_file()
    )


def _evidence(tmp_path: Path) -> dict[str, Any]:
    return json.loads(
        (tmp_path / "bundle/stages/composition/composition-evidence.json").read_text()
    )


def _validate_bundle(tmp_path: Path) -> None:
    validated = subprocess.run(
        [
            "sdi-integration",
            "validate-bundle",
            "--bundle-root",
            str(tmp_path / "bundle"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert validated.returncode == 0, validated.stderr


@pytest.mark.live
def test_composes_the_waffle_orin_run_with_a_real_model(tmp_path: Path) -> None:
    generator = live_generator()
    keys = live_keys(generator)
    if keys is None:
        pytest.skip(f"the {generator} or embeddings key is unavailable")
    commit_sha = _repository(tmp_path)

    completed = _dispatch(tmp_path, commit_sha, generator=generator, keys=keys)

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    composition, image_build, cv, cd = result["attempts"]
    assert composition["implementation_mode"] == "implemented"
    assert composition["execution_conclusion"] == "succeeded"
    if composition["domain_outcome"] == "succeeded":
        assert composition["reason"]["code"] == "sdi.composition.preferred-proposal"
        assert _composition_files(result) == {
            "composition_blueprint",
            "deployment_schema",
            "composition_evidence",
            "diagnostic",
        }
        assert image_build["reason"]["code"] == "sdi.dependency.implemented-evidence"
    else:
        assert composition["domain_outcome"] == "failed"
        assert composition["reason"]["code"] in NO_RESULT_CODES
        assert _composition_files(result) == {"composition_evidence", "diagnostic"}
        assert image_build["reason"]["code"] == "sdi.dependency.prerequisite-blocked"
    assert image_build["lifecycle_state"] == "skipped"
    assert [cv["lifecycle_state"], cd["lifecycle_state"]] == ["skipped", "skipped"]
    _validate_bundle(tmp_path)
    for api_key in keys.values():
        assert api_key not in (
            _bundle_text(tmp_path) + completed.stdout + completed.stderr
        )
    evidence = _evidence(tmp_path)
    assert evidence["generation"]["generator"] == generator
    assert evidence["discovery"]["dense"]["status"] == "used"
    if composition["reason"]["code"] == "sdi.composition.bounded-no-result":
        return
    proposals = cast(
        "list[dict[str, list[dict[str, object]] | None]]",
        evidence["assessment"]["proposals"],
    )
    for proposal in proposals:
        for service in proposal["services"] or []:
            assert (service["route"] is None) == (service["artifact_id"] is None)


@pytest.mark.live
def test_exhausted_output_is_a_bounded_domain_failure(tmp_path: Path) -> None:
    api_key = openai_key()
    if api_key is None:
        pytest.skip("no OpenAI key is available")
    # luna, the default and first generator, gets the bounded output.
    bounded = COMPOSITION_CONFIG.read_text().replace(
        "max_output_tokens: 32000", "max_output_tokens: 16", 1
    )
    commit_sha = _repository(tmp_path, {CONFIG_PATH: bounded})

    completed = _dispatch(tmp_path, commit_sha, keys={"OPENAI_API_KEY": api_key})

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    composition = result["attempts"][0]
    assert composition["execution_conclusion"] == "succeeded"
    assert composition["domain_outcome"] == "failed"
    assert composition["reason"]["code"] == "sdi.composition.bounded-no-result"
    assert _composition_files(result) == {"composition_evidence", "diagnostic"}
    evidence = _evidence(tmp_path)
    assert evidence["generation"]["outcome"] == "exhausted"
    assert evidence["generation"]["generator"] == "luna"
    assert evidence["generation"]["max_output_tokens"] == 16
    _validate_bundle(tmp_path)
    assert api_key not in _bundle_text(tmp_path)


@pytest.mark.live
@pytest.mark.parametrize(
    ("lexical_only", "code"),
    [
        (True, "sdi.composition.provider-error"),
        # Declared embeddings reject the shared key before generation runs.
        (False, "sdi.composition.dense-discovery-unavailable"),
    ],
)
def test_a_rejected_key_fails_execution_without_outputs(
    tmp_path: Path, *, lexical_only: bool, code: str
) -> None:
    api_key = "sk-test-only-invalid"
    commit_sha = _repository(
        tmp_path, {CONFIG_PATH: lexical_only_config()} if lexical_only else {}
    )

    completed = _dispatch(tmp_path, commit_sha, keys={"OPENAI_API_KEY": api_key})

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    composition, image_build, *_ = result["attempts"]
    assert composition["execution_conclusion"] == "failed"
    assert composition["domain_outcome"] == "not_evaluated"
    assert composition["reason"]["code"] == code
    assert _composition_files(result) == {"diagnostic"}
    assert image_build["reason"]["code"] == "sdi.dependency.prerequisite-blocked"
    _validate_bundle(tmp_path)
    assert api_key not in _bundle_text(tmp_path) + completed.stdout


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("refusal", "sdi.composition.generation-refused"),
        ("off-schema", "sdi.composition.generation-invalid"),
    ],
)
def test_an_answer_without_proposals_fails_execution(
    tmp_path: Path, body: str, code: str
) -> None:
    completed = _dispatch_stub(tmp_path, BODIES[body])

    assert completed.returncode == 1, completed.stderr
    result = json.loads(completed.stdout)
    composition = result["attempts"][0]
    assert composition["execution_conclusion"] == "failed"
    assert composition["domain_outcome"] == "not_evaluated"
    assert composition["reason"]["code"] == code
    assert _composition_files(result) == {"diagnostic"}
    diagnostic = (tmp_path / "bundle/stages/composition/diagnostic.txt").read_text()
    config_sha256 = hashlib.sha256(
        (tmp_path / "repository" / CONFIG_PATH).read_bytes()
    ).hexdigest()
    for fact in (
        f"composition_config_sha256: {config_sha256}",
        "generator: stub",
        "model: stub/model",
        "reasoning_effort: low",
        "max_output_tokens: 1000",
        "timeout_seconds: 30",
        "omitted: composition evidence, which failed attempts do not publish",
    ):
        assert f"\n{fact}\n" in diagnostic
    _validate_bundle(tmp_path)
    assert STUB_KEY not in _bundle_text(tmp_path) + completed.stdout


def test_publishes_the_preferred_stubbed_proposal(tmp_path: Path) -> None:
    completed = _dispatch_stub(tmp_path, proposals_body([COMPLETE]))

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    composition, image_build, *_ = result["attempts"]
    assert composition["execution_conclusion"] == "succeeded"
    assert composition["domain_outcome"] == "succeeded"
    assert composition["reason"]["code"] == "sdi.composition.preferred-proposal"
    evidence = _evidence(tmp_path)
    assert evidence["generation"]["generator"] == "stub"
    assert evidence["generation"]["composition_config_sha256"] == (
        hashlib.sha256((tmp_path / "repository" / CONFIG_PATH).read_bytes()).hexdigest()
    )
    assert evidence["generation"]["base_url"].startswith("http://127.0.0.1:")
    assert "api_key_env" not in evidence["generation"]
    discovery = evidence["discovery"]
    assert discovery["dense"] == {"status": "not-configured"}
    assert {item["service_id"] for item in COMPLETE["services"]} <= {
        item["service_id"] for item in discovery["selected"]
    }
    assert _composition_files(result) == {
        "composition_blueprint",
        "deployment_schema",
        "composition_evidence",
        "diagnostic",
    }
    assert image_build["reason"]["code"] == "sdi.dependency.implemented-evidence"
    blueprint = json.loads(
        (tmp_path / "bundle/stages/composition/composition-blueprint.json").read_text()
    )
    assert {item["service_id"]: item["basis"] for item in blueprint["services"]} == {
        item["service_id"]: (
            "placeholder"
            if "basis: placeholder"
            in (SERVICE_REPOSITORY / item["service_id"] / "SDI.md").read_text()
            else "declared"
        )
        for item in COMPLETE["services"]
    }
    deployment = json.loads(
        (tmp_path / "bundle/stages/composition/deployment-schema.json").read_text()
    )
    assert {
        blueprint["profile_basis"],
        deployment["profile_basis"],
        evidence["profile_basis"],
    } == {"placeholder"}
    _validate_bundle(tmp_path)
    assert STUB_KEY not in _bundle_text(tmp_path) + completed.stdout


def test_publishes_only_evidence_without_an_eligible_proposal(
    tmp_path: Path,
) -> None:
    ineligible = copy.deepcopy(COMPLETE)
    ineligible["coverage"][0] |= {"status": "missing", "services": ["v4l2-camera"]}

    completed = _dispatch_stub(tmp_path, proposals_body([ineligible]))

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    composition, image_build, *_ = result["attempts"]
    assert composition["execution_conclusion"] == "succeeded"
    assert composition["domain_outcome"] == "failed"
    assert composition["reason"]["code"] == "sdi.composition.insufficient-proposals"
    assert _composition_files(result) == {"composition_evidence", "diagnostic"}
    assert image_build["reason"]["code"] == "sdi.dependency.prerequisite-blocked"
    _validate_bundle(tmp_path)
    assert STUB_KEY not in _bundle_text(tmp_path) + completed.stdout


def test_a_result_within_a_bounded_context_says_so(tmp_path: Path) -> None:
    navigation_only: dict[str, object] = {
        "rationale": "Only navigation fits the context budget.",
        "services": [
            {
                "service_id": "nav2-navigation",
                "artifact_id": "jazzy-debs",
                "host": "orin",
                "capability": "path planning",
            }
        ],
        # Missing coverage that cites a service makes the proposal ineligible.
        "coverage": [
            entry
            | {
                "status": "missing",
                "services": ["nav2-navigation"]
                if entry["requirement_id"] == "return-to-origin"
                else [],
            }
            for entry in COMPLETE["coverage"]
        ],
    }

    completed = _dispatch_stub(
        tmp_path, proposals_body([navigation_only]), max_context_bytes=4000
    )

    assert completed.returncode == 0, completed.stderr
    composition = json.loads(completed.stdout)["attempts"][0]
    assert composition["reason"]["code"] == "sdi.composition.insufficient-proposals"
    evidence = _evidence(tmp_path)
    assert evidence["discovery"]["complete"] is False
    assert evidence["assessment"]["reason"] == (
        "no proposal is eligible within the bounded supplied context; "
        "see discovery.omitted"
    )
    diagnostic = (tmp_path / "bundle/stages/composition/diagnostic.txt").read_text()
    assert "\ndiscovery_selected: 1\n" in diagnostic
    _validate_bundle(tmp_path)


def test_a_budget_no_description_fits_fails_before_generation(
    tmp_path: Path,
) -> None:
    completed = _dispatch_stub(
        tmp_path, proposals_body([COMPLETE]), max_context_bytes=100
    )

    assert completed.returncode == 1, completed.stderr
    composition = json.loads(completed.stdout)["attempts"][0]
    assert composition["execution_conclusion"] == "failed"
    assert composition["reason"]["code"] == "sdi.composition.discovery-over-budget"
    assert _composition_files(json.loads(completed.stdout)) == {"diagnostic"}
    diagnostic = (tmp_path / "bundle/stages/composition/diagnostic.txt").read_text()
    assert "\ndiscovery_selected: 0\n" in diagnostic
    assert "\ndiscovery_over_budget: 0\n" not in diagnostic
    _validate_bundle(tmp_path)


def test_an_invalid_description_fails_before_generation(tmp_path: Path) -> None:
    service_repository = tmp_path / "service-repository"
    shutil.copytree(SERVICE_REPOSITORY, service_repository)
    (service_repository / "broken").mkdir()
    (service_repository / "broken/SDI.md").write_text(
        "---\nschema_version: sdi.service-description/v1\nservice_id: broken\n---\n"
    )
    commit_sha = _repository(tmp_path)

    completed = _dispatch(tmp_path, commit_sha, service_repository=service_repository)

    assert completed.returncode == 1, completed.stderr
    composition = json.loads(completed.stdout)["attempts"][0]
    assert composition["execution_conclusion"] == "failed"
    assert composition["reason"]["code"] == (
        "sdi.composition.invalid-service-description"
    )
    diagnostic = (tmp_path / "bundle/stages/composition/diagnostic.txt").read_text()
    assert "invalid_description: broken/SDI.md" in diagnostic


def test_declared_embeddings_that_fail_end_the_stage_before_generation(
    tmp_path: Path,
) -> None:
    completed = _dispatch_stub(
        tmp_path,
        proposals_body([COMPLETE]),
        embeddings={"model": UNAVAILABLE_EMBEDDINGS},
    )

    assert completed.returncode == 1, completed.stderr
    composition = json.loads(completed.stdout)["attempts"][0]
    assert composition["execution_conclusion"] == "failed"
    assert composition["reason"]["code"] == (
        "sdi.composition.dense-discovery-unavailable"
    )
    diagnostic = (tmp_path / "bundle/stages/composition/diagnostic.txt").read_text()
    assert "\ndense_status: failed\n" in diagnostic
    assert "generation_outcome" not in diagnostic
    _validate_bundle(tmp_path)


def test_allowed_lexical_only_discovery_is_in_the_stage_summary(
    tmp_path: Path,
) -> None:
    completed = _dispatch_stub(
        tmp_path,
        proposals_body([COMPLETE]),
        embeddings={"model": UNAVAILABLE_EMBEDDINGS, "on_unavailable": "lexical"},
    )

    assert completed.returncode == 0, completed.stderr
    composition = json.loads(completed.stdout)["attempts"][0]
    assert composition["reason"]["code"] == "sdi.composition.preferred-proposal"
    assert composition["reason"]["summary"].endswith(
        "Embeddings were unavailable, so discovery was lexical only."
    )
    assert _evidence(tmp_path)["discovery"]["dense"]["status"] == "failed"
    _validate_bundle(tmp_path)


@pytest.mark.parametrize(
    "defect", ["symlinked-description", "unbound-key", "unknown-generator"]
)
def test_rejects_unsafe_composition_inputs_before_launch(
    tmp_path: Path, defect: str
) -> None:
    service_repository = tmp_path / "service-repository"
    shutil.copytree(SERVICE_REPOSITORY, service_repository)
    extra: dict[str, str] = {}
    generator = None
    if defect == "symlinked-description":
        (service_repository / "linked").mkdir()
        (service_repository / "linked/SDI.md").symlink_to(
            service_repository / "nav2-navigation/SDI.md"
        )
    elif defect == "unbound-key":
        extra[CONFIG_PATH] = COMPOSITION_CONFIG.read_text().replace(
            "api_key_env: OPENAI_API_KEY", "api_key_env: OTHER_KEY", 1
        )
    else:
        generator = "absent"
    commit_sha = _repository(tmp_path, extra)

    completed = _dispatch(
        tmp_path,
        commit_sha,
        generator=generator,
        service_repository=service_repository,
        keys={"OTHER_KEY": "sk-test-only-other"},
    )

    assert completed.returncode == 2
    assert not (tmp_path / "bundle").exists()
