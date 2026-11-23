"""Public-boundary checks for model-generated composition proposals.

The live checks call a real model: the self-hosted qwen generator while its
endpoint serves it, else luna. They assert the command's contract, never what
the model chose or how it worded it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from tests._live_model import (
    COMPOSITION_CONFIG,
    REPOSITORY_ROOT,
    lexical_only_config,
    live_generator,
    live_keys,
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

SERVICE_REPOSITORY = REPOSITORY_ROOT / "service-repository"
PROFILE = REPOSITORY_ROOT / "profiles/s-04/waffle-jetson-arm64.yaml"
REQUIREMENTS = REPOSITORY_ROOT / "requirements/s-04/deliver-book-to-joe.md"
INPUTS = [
    "--target-profile",
    str(PROFILE),
    "--requirements-specification",
    str(REQUIREMENTS),
]


COVERAGE_IDS = [entry["requirement_id"] for entry in COMPLETE["coverage"]]
UNRELATED = """\
---
schema_version: sdi.service-description/v1
service_id: fake-irrigation
title: Lawn sprinkler valve scheduler
provenance:
  basis: placeholder
  note: Hypothetical placeholder for discovery checks.
artifacts:
  vendor-image:
    architectures: [amd64]
    route: {kind: image, reference: example.invalid/sprinkler:v1}
---

## Purpose

Opens garden sprinkler valves on a watering timetable, reading soil moisture
probes and rainfall forecasts to skip irrigation during heavy rain.
"""


def _generate(
    tmp_path: Path,
    config: Path,
    env: dict[str, str],
    service_repository: Path = SERVICE_REPOSITORY,
    generator: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sdi-integration",
            "generate-proposals",
            "--service-repository",
            str(service_repository),
            *INPUTS,
            "--composition-config",
            str(config),
            *([] if generator is None else ["--generator", generator]),
            "--output",
            str(tmp_path / "proposals.json"),
            "--evidence",
            str(tmp_path / "evidence.json"),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | env,
    )


def test_generates_proposals_the_assessment_accepts(tmp_path: Path) -> None:
    generator = live_generator()
    keys = live_keys(generator)
    if keys is None:
        pytest.skip(f"the {generator} or embeddings key is unavailable")

    completed = _generate(tmp_path, COMPOSITION_CONFIG, keys, generator=generator)

    assert completed.returncode == 0, completed.stderr
    proposals = json.loads((tmp_path / "proposals.json").read_text())
    assert 1 <= len(proposals["proposals"]) <= 3
    assessed = subprocess.run(
        [
            "sdi-integration",
            "assess-proposals",
            "--service-repository",
            str(SERVICE_REPOSITORY),
            *INPUTS,
            "--proposals",
            str(tmp_path / "proposals.json"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert assessed.returncode == 0, assessed.stderr
    evidence_text = (tmp_path / "evidence.json").read_text()
    evidence = json.loads(evidence_text)
    assert evidence["outcome"] == "generated"
    assert evidence["settings"]["generator"] == generator
    assert evidence["request"]["model"] == evidence["settings"]["model"]
    assert evidence["request"]["text"]["format"]["strict"] is True
    assert evidence["response"]["status"] == "completed"
    assert evidence["discovery"]["dense"]["status"] == "used"
    for api_key in keys.values():
        assert api_key not in evidence_text + completed.stdout + completed.stderr


def test_provider_error_writes_no_proposals(tmp_path: Path) -> None:
    generator = live_generator()
    config = tmp_path / "config.yaml"
    config.write_text(lexical_only_config())
    key_name = "VLLM_KEY" if generator == "qwen" else "OPENAI_API_KEY"
    api_key = "sk-test-only-invalid"
    completed = _generate(tmp_path, config, {key_name: api_key}, generator=generator)

    assert completed.returncode == 1, completed.stderr
    assert "provider_error" in completed.stderr
    assert not (tmp_path / "proposals.json").exists()
    evidence_text = (tmp_path / "evidence.json").read_text()
    evidence = json.loads(evidence_text)
    assert evidence["outcome"] == "provider_error"
    assert evidence["error"]["status_code"] == 401
    assert api_key not in evidence_text + completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("body", "outcome"),
    [
        ("content-filter", "refused"),
        ("refusal", "refused"),
        ("off-schema", "invalid"),
        ("failed", "provider_error"),
    ],
)
def test_reports_why_the_provider_returned_no_proposals(
    tmp_path: Path, body: str, outcome: str
) -> None:
    config = tmp_path / "stub.yaml"
    with stub_provider(BODIES[body]) as base_url:
        config.write_text(stub_config(base_url))
        completed = _generate(tmp_path, config, {"VLLM_KEY": STUB_KEY})

    assert completed.returncode == 1, completed.stderr
    assert not (tmp_path / "proposals.json").exists()
    evidence_text = (tmp_path / "evidence.json").read_text()
    evidence = json.loads(evidence_text)
    assert evidence["outcome"] == outcome
    assert evidence["request"]["model"] == "stub/model"
    assert evidence["response"]["id"] == "resp_stub"
    assert STUB_KEY not in evidence_text + completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("  luna:\n", "  luna:\n    temperature: 0\n"), "temperature"),
        (
            ("default_generator: luna", "default_generator: absent"),
            "default_generator must name a defined generator",
        ),
        (("  embeddings:\n", "  embeddings:\n    on_unavailable: skip\n"), "skip"),
    ],
)
def test_rejects_an_invalid_composition_config(
    tmp_path: Path, change: tuple[str, str], message: str
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(COMPOSITION_CONFIG.read_text().replace(*change, 1))

    completed = _generate(tmp_path, config, {})

    assert completed.returncode == 2
    assert message in completed.stderr
    assert not (tmp_path / "evidence.json").exists()


def test_rejects_a_generator_the_config_does_not_name(tmp_path: Path) -> None:
    completed = _generate(tmp_path, COMPOSITION_CONFIG, {}, generator="absent")

    assert completed.returncode == 2
    assert "unknown generator 'absent'; the config defines luna, qwen" in (
        completed.stderr
    )
    assert not (tmp_path / "evidence.json").exists()


def _stub_generate(  # noqa: PLR0913
    tmp_path: Path,
    body: dict[str, object],
    *,
    service_repository: Path = SERVICE_REPOSITORY,
    keys: dict[str, str] | None = None,
    generator: str | None = None,
    received: list[tuple[str, str]] | None = None,
    **config: Any,  # noqa: ANN401
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    path = tmp_path / "stub.yaml"
    keys = {"VLLM_KEY": STUB_KEY} if keys is None else keys
    with stub_provider(body, received) as base_url:
        path.write_text(stub_config(base_url, **config))
        completed = _generate(tmp_path, path, keys, service_repository, generator)
    evidence_text = (tmp_path / "evidence.json").read_text()
    for api_key in keys.values():
        assert api_key not in evidence_text + completed.stdout + completed.stderr
    return completed, json.loads(evidence_text)


def test_the_model_sees_and_may_choose_only_discovered_services(
    tmp_path: Path,
) -> None:
    service_repository = tmp_path / "services"
    shutil.copytree(SERVICE_REPOSITORY, service_repository)
    (service_repository / "fake-irrigation").mkdir()
    (service_repository / "fake-irrigation/SDI.md").write_text(UNRELATED)
    choosing_the_omitted = copy.deepcopy(COMPLETE)
    choosing_the_omitted["services"][0]["service_id"] = "fake-irrigation"

    completed, evidence = _stub_generate(
        tmp_path,
        proposals_body([choosing_the_omitted]),
        service_repository=service_repository,
    )

    assert completed.returncode == 1
    assert evidence["outcome"] == "invalid"
    discovery = evidence["discovery"]
    assert {"service_id": "fake-irrigation", "reason": "not retrieved"} in (
        discovery["omitted"]
    )
    assert discovery["complete"] is False
    assert discovery["dense"] == {"status": "not-configured"}
    assert [query["query_id"] for query in discovery["queries"]] == [
        *COVERAGE_IDS,
        "profile",
    ]
    prompt = evidence["request"]["input"]
    omitted = prompt.split("<omitted>")[1].split("</omitted>")[0]
    assert "fake-irrigation" in omitted.split(", ")
    assert 'id="fake-irrigation"' not in prompt
    assert "fake-irrigation" not in json.dumps(evidence["request"]["text"])


def test_the_context_budget_bounds_discovery_and_says_what_it_left_out(
    tmp_path: Path,
) -> None:
    completed, evidence = _stub_generate(
        tmp_path, BODIES["off-schema"], max_context_bytes=4000
    )

    assert completed.returncode == 1
    assert evidence["settings"]["max_context_bytes"] == 4000
    discovery = evidence["discovery"]
    # The orchestrator's navigation goal admits navigation; its inputs no longer fit.
    assert [item["service_id"] for item in discovery["selected"]] == ["nav2-navigation"]
    assert discovery["selected"][0]["reason"] == (
        "provides action_clients /navigate_to_pose of orchestrator"
    )
    assert discovery["budget"]["used"] <= 4000
    assert {"service_id": "nav2-localization", "reason": "budget"} in (
        discovery["omitted"]
    )
    assert {
        "service_id": "nav2-navigation",
        "reference": "subscribes /map",
        "providers": ["nav2-localization"],
    } in discovery["unexpanded_references"]
    assert discovery["complete"] is False


def test_dense_ranking_joins_lexical_ranking_when_embeddings_are_configured(
    tmp_path: Path,
) -> None:
    completed, evidence = _stub_generate(tmp_path, BODIES["off-schema"], embeddings={})

    assert completed.returncode == 1
    discovery = evidence["discovery"]
    assert discovery["dense"]["status"] == "used"
    assert discovery["dense"]["model"] == "stub/embeddings"
    for query in discovery["queries"]:
        assert all(isinstance(item["dense"], int) for item in query["ranked"])


@pytest.mark.parametrize(
    ("embeddings", "status"),
    [
        ({"api_key_env": "SDI_TEST_UNSET_KEY"}, "key-unset"),
        ({"model": UNAVAILABLE_EMBEDDINGS}, "failed"),
    ],
)
def test_declared_embeddings_that_are_unavailable_fail_before_generation(
    tmp_path: Path, embeddings: dict[str, str], status: str
) -> None:
    received: list[tuple[str, str]] = []
    completed, evidence = _stub_generate(
        tmp_path, BODIES["off-schema"], embeddings=embeddings, received=received
    )

    assert completed.returncode == 1
    assert f"dense-discovery-unavailable: embeddings {status}" in completed.stderr
    assert evidence["outcome"] == "dense-discovery-unavailable"
    assert evidence["discovery"]["dense"]["status"] == status
    assert "request" not in evidence
    assert not any(path.endswith("/responses") for path, _ in received)


def test_embeddings_may_allow_lexical_only_discovery_and_say_so(
    tmp_path: Path,
) -> None:
    completed, evidence = _stub_generate(
        tmp_path,
        BODIES["off-schema"],
        embeddings={"api_key_env": "SDI_TEST_UNSET_KEY", "on_unavailable": "lexical"},
    )

    assert completed.returncode == 1
    assert "embeddings key-unset; discovery is lexical only" in completed.stderr
    assert evidence["outcome"] == "invalid"
    assert evidence["discovery"]["dense"]["status"] == "key-unset"


def test_a_named_generator_replaces_the_default(tmp_path: Path) -> None:
    received: list[tuple[str, str]] = []
    # The default generator is unreachable; naming another must not touch it.
    other = (
        "  other:\n"
        "    model: other/model\n"
        "    reasoning_effort: high\n"
        "    max_output_tokens: 2000\n"
        "    timeout_seconds: 60\n"
        "    max_context_bytes: 50000\n"
        "    base_url: {base_url}\n"
        "    api_key_env: SDI_TEST_OTHER_KEY\n"
    )
    path = tmp_path / "stub.yaml"
    with stub_provider(BODIES["off-schema"], received) as base_url:
        path.write_text(
            stub_config("http://127.0.0.1:9/v1", generators=other).replace(
                "{base_url}", base_url
            )
        )
        completed = _generate(
            tmp_path,
            path,
            {"SDI_TEST_OTHER_KEY": "sk-test-only-other"},
            generator="other",
        )

    assert completed.returncode == 1, completed.stderr
    evidence = json.loads((tmp_path / "evidence.json").read_text())
    assert evidence["outcome"] == "invalid"
    assert evidence["settings"]["generator"] == "other"
    assert evidence["settings"]["max_context_bytes"] == 50000
    assert evidence["settings"]["composition_config_sha256"] == (
        hashlib.sha256(path.read_bytes()).hexdigest()
    )
    assert "api_key_env" not in evidence["settings"]
    assert evidence["request"]["model"] == "other/model"
    assert received == [("/v1/responses", "Bearer sk-test-only-other")]


def test_each_endpoint_receives_only_its_own_key(tmp_path: Path) -> None:
    received: list[tuple[str, str]] = []
    keys = {
        "VLLM_KEY": "sk-test-only-generator",
        "OPENAI_API_KEY": "sk-test-only-dense",
    }

    completed, evidence = _stub_generate(
        tmp_path,
        BODIES["off-schema"],
        keys=keys,
        embeddings={"api_key_env": "OPENAI_API_KEY"},
        received=received,
    )

    assert completed.returncode == 1
    assert evidence["discovery"]["dense"]["status"] == "used"
    assert sorted(received) == [
        ("/v1/embeddings", "Bearer sk-test-only-dense"),
        ("/v1/responses", "Bearer sk-test-only-generator"),
    ]
