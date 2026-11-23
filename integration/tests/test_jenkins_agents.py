"""Black-box checks for the repository-owned Jenkins agent deployment."""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from ruamel.yaml import YAML

from sdi_pipeline_integration._integration_code import BAKED_ROOTS
from sdi_pipeline_integration._stage_registry import AGENTS, STAGES

if TYPE_CHECKING:
    from collections.abc import Mapping


REPOSITORY_ROOT = Path(__file__).parents[2]
JENKINS_ROOT = REPOSITORY_ROOT / "deployment" / "jenkins"
AGENT_NAMES = {agent.node for agent in AGENTS}
BUILDER_SERVICES = {"buildkitd", "registry"}
RUNNER_STACK_SERVICES = {
    "runner-upstream",
    "runner-fork",
    "runner-upstream-proxy",
    "runner-fork-proxy",
}
AGENT_LABELS = {agent.node: agent.label for agent in AGENTS}
AGENT_TARGETS = dict.fromkeys(AGENT_NAMES, "agent") | {"image-build": "image-build"}
AGENT_NETWORKS = {name: [f"controller-{name}"] for name in AGENT_NAMES} | {
    "image-build": ["controller-image-build", "image-build-builder"]
}
FIXTURE_IMAGE = (
    "docker.io/library/python@"
    "sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36"
)


def _load_yaml(path: Path) -> Mapping[str, Any]:
    loaded = YAML(typ="safe").load(path)  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(loaded, dict)
    return cast("Mapping[str, Any]", loaded)


def _installation_pins() -> dict[str, str]:
    lines = (REPOSITORY_ROOT / "deployment" / "installation.env").read_text()
    return dict(
        line.split("=", maxsplit=1)
        for line in lines.splitlines()
        if line and not line.startswith("#")
    )


def _inventory(
    *,
    labels: Mapping[str, str] = AGENT_LABELS,
    executors: Mapping[str, int] | None = None,
    offline: set[str] | None = None,
    busy: set[str] | None = None,
) -> dict[str, Any]:
    executors = executors or {}
    offline = offline or set()
    busy = busy or set()
    return {
        "computer": [
            {
                "displayName": name,
                "numExecutors": executors.get(name, 1),
                "offline": name in offline,
                "temporarilyOffline": False,
                "idle": name not in busy,
                "explicitLabels": labels[name],
            }
            for name in AGENT_LABELS
        ]
    }


def _run_preflight(
    tmp_path: Path, inventory: Mapping[str, Any]
) -> subprocess.CompletedProcess[str]:
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(inventory))
    return subprocess.run(
        [
            JENKINS_ROOT / "bin" / "smoke-check",
            "preflight",
            "--inventory",
            inventory_path,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_compose_defines_five_isolated_websocket_agents() -> None:
    compose = _load_yaml(JENKINS_ROOT / "compose.yaml")
    services = compose["services"]

    assert set(services) == {
        "controller",
        *AGENT_NAMES,
        *BUILDER_SERVICES,
        *RUNNER_STACK_SERVICES,
    }
    assert "50000" not in (JENKINS_ROOT / "compose.yaml").read_text()

    controller_networks = set(services["controller"]["networks"])
    agent_networks = {f"controller-{name}" for name in AGENT_NAMES}
    assert agent_networks < controller_networks

    registration_secrets: set[str] = set()
    for name in AGENT_NAMES:
        agent = services[name]
        assert agent["platform"] == "linux/amd64"
        assert agent["restart"] == "no"
        assert agent["init"] is True
        assert agent["read_only"] is True
        assert agent["cap_drop"] == ["ALL"]
        assert agent["security_opt"] == ["no-new-privileges:true"]
        assert agent["environment"]["JENKINS_WEB_SOCKET"] == "true"
        assert agent["environment"]["JENKINS_AGENT_NAME"] == name
        assert agent["environment"]["JENKINS_URL"] == "${JENKINS_AGENT_CONTROLLER_URL}"
        assert agent.get("volumes", []) == []
        assert {entry.split(":", maxsplit=1)[0] for entry in agent["tmpfs"]} == {
            "/home/jenkins/agent",
            "/tmp",  # noqa: S108 - container mount point, not a host temp file.
        }
        assert agent["networks"] == AGENT_NETWORKS[name]
        assert agent["build"]["target"] == AGENT_TARGETS[name]
        assert (
            services[name]["depends_on"]["controller"]["condition"] == "service_healthy"
        )
        assert len(agent["secrets"]) == 1
        registration_secrets.add(agent["secrets"][0])

        expected_role = "integration" if name == "integration" else "domain"
        expected_label = "" if name == "integration" else AGENT_LABELS[name]
        assert agent["build"]["args"]["SDI_JENKINS_AGENT_ROLE"] == expected_role
        assert agent["build"]["args"]["SDI_JENKINS_AGENT_LABEL"] == expected_label
        assert "SDI_JENKINS_AGENT_ROLE" not in agent["environment"]
        assert "SDI_JENKINS_AGENT_LABEL" not in agent["environment"]

    assert len(registration_secrets) == 5
    assert all(
        compose["networks"][name]["internal"]
        == "${JENKINS_AGENT_NETWORK_INTERNAL:-false}"
        for name in agent_networks
    )
    assert all("docker.sock" not in json.dumps(services[name]) for name in services)


def test_domain_agents_use_their_descriptor_selected_runtime_image() -> None:
    compose = _load_yaml(JENKINS_ROOT / "compose.yaml")
    services = compose["services"]
    for name, stage in STAGES.items():
        descriptor = _load_yaml(REPOSITORY_ROOT / stage.jenkins_descriptor)
        assert descriptor["image"] == FIXTURE_IMAGE
        assert services[stage.agent.node]["build"]["args"]["RUNTIME_IMAGE"] == (
            f"${{JENKINS_{name.upper()}_AGENT_IMAGE}}"
        )

    dockerfile = (JENKINS_ROOT / "agents" / "Dockerfile").read_text()
    assert "FROM ${RUNTIME_IMAGE} AS runtime" in dockerfile
    assert "FROM ${REMOTING_IMAGE} AS remoting" in dockerfile
    assert (
        "FROM runtime AS agent\nLABEL org.sdi.project=ci-cv-cd_framework\n"
        in dockerfile
    )
    assert "FROM ${BUILDKIT_IMAGE} AS buildkit" in dockerfile
    assert dockerfile.endswith(
        "FROM agent AS image-build\n"
        "COPY --from=buildkit /usr/bin/buildctl /usr/bin/buildctl\n"
    )
    assert "FROM ${UV_IMAGE} AS uv" in dockerfile
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "/etc/sdi/jenkins-agent-role" in dockerfile
    assert "/etc/sdi/jenkins-agent-label" in dockerfile


def test_builder_and_registry_are_reachable_only_from_the_image_build_agent() -> None:
    compose = _load_yaml(JENKINS_ROOT / "compose.yaml")
    services = compose["services"]
    networks = compose["networks"]
    pins = _installation_pins()
    buildkitd = services["buildkitd"]
    registry = services["registry"]

    assert buildkitd["image"] == pins["SDI_BUILDKIT_IMAGE"]
    assert registry["image"] == pins["SDI_REGISTRY_IMAGE"]
    for service in (buildkitd, registry):
        assert service["platform"] == "linux/amd64"
        assert service["restart"] == "no"
        assert "ports" not in service
        assert "build" not in service
        assert service["healthcheck"]["test"][0] == "CMD"
    assert buildkitd["privileged"] is True
    assert buildkitd["command"] == [
        "--addr",
        "tcp://0.0.0.0:1234",
        "--config",
        "/etc/buildkit/buildkitd.toml",
    ]
    assert buildkitd["networks"] == ["image-build-builder", "builder-egress"]
    assert registry["networks"] == ["image-build-builder"]
    assert registry["read_only"] is True
    assert registry["cap_drop"] == ["ALL"]
    assert registry["security_opt"] == ["no-new-privileges:true"]

    # Each named volume covers the path its upstream image declares as VOLUME, so
    # Docker creates no anonymous volume that survives `stop`.
    assert buildkitd["volumes"] == [
        "buildkit-state:/var/lib/buildkit",
        "./buildkit/buildkitd.toml:/etc/buildkit/buildkitd.toml:ro",
    ]
    assert registry["volumes"] == ["pipeline-registry:/var/lib/registry"]
    assert {"buildkit-state", "pipeline-registry"} <= set(compose["volumes"])

    assert networks["image-build-builder"]["internal"] is True
    assert networks["builder-egress"] is None
    members = {
        name: set(service.get("networks", [])) for name, service in services.items()
    }
    assert {
        name for name, nets in members.items() if "image-build-builder" in nets
    } == {
        "image-build",
        *BUILDER_SERVICES,
    }
    assert {name for name, nets in members.items() if "builder-egress" in nets} == {
        "buildkitd"
    }


def test_buildkitd_config_trusts_only_the_pipeline_registry_without_entitlements() -> (
    None
):
    config_text = (JENKINS_ROOT / "buildkit" / "buildkitd.toml").read_text()
    config = tomllib.loads(config_text)

    assert config["registry"] == {"registry:5000": {"http": True}}
    assert "entitlements" not in config
    assert "insecure" not in config_text
    policies = config["worker"]["oci"]["gcpolicy"]
    assert config["worker"]["oci"]["gc"] is True
    assert len(policies) == 1
    assert policies[0]["all"] is True
    assert "keepDuration" not in policies[0]
    assert {"maxUsedSpace", "reservedSpace", "minFreeSpace"} <= set(policies[0])


@pytest.mark.parametrize("image", ["agents", "runner"])
def test_images_digest_exactly_the_integration_code_they_bake(image: str) -> None:
    dockerfile = (JENKINS_ROOT / image / "Dockerfile").read_text()
    copied = {
        source.removeprefix("integration/")
        for line in dockerfile.splitlines()
        if line.startswith("COPY integration/")
        for source in line.split()[1:-1]
    }
    dockerignore = (
        (JENKINS_ROOT / image / "Dockerfile.dockerignore").read_text().splitlines()
    )

    assert copied - {"README.md"} == set(BAKED_ROOTS)
    for root in BAKED_ROOTS:
        assert f"!integration/{root}" in dockerignore or (
            f"!integration/{root}/**" in dockerignore
        )
    assert dockerignore[-2:] == ["integration/**/__pycache__", "integration/**/*.pyc"]
    assert "integration-code-digest --root ." in dockerfile
    assert "/etc/sdi/integration-code-digest" in dockerfile


def test_runner_and_agents_share_one_cached_integration_environment() -> None:
    def tooling_stage(image: str) -> str:
        dockerfile = (JENKINS_ROOT / image / "Dockerfile").read_text()
        stage = dockerfile.split("FROM runtime AS tooling\n", maxsplit=1)[1]
        return stage.split("\n\nFROM ", maxsplit=1)[0]

    assert tooling_stage("runner") == tooling_stage("agents")


def test_jcasc_owns_exact_agent_identities_labels_and_executors() -> None:
    casc = _load_yaml(JENKINS_ROOT / "casc" / "jenkins.yaml")
    nodes = casc["jenkins"]["nodes"]
    by_name = {node["permanent"]["name"]: node["permanent"] for node in nodes}

    assert set(by_name) == AGENT_NAMES
    for name, label in AGENT_LABELS.items():
        node = by_name[name]
        assert node["numExecutors"] == 1
        assert node["mode"] == "EXCLUSIVE"
        assert node["labelString"] == label
        assert node["remoteFS"] == "/home/jenkins/agent"
        assert node["launcher"]["inbound"]["workDirSettings"] == {
            "disabled": False,
            "failIfWorkDirIsMissing": True,
            "internalDir": "remoting",
            "workDirPath": "/home/jenkins/agent",
        }
        assert node["retentionStrategy"] == "always"


@pytest.mark.parametrize(
    ("inventory", "category"),
    [
        (_inventory(labels={**AGENT_LABELS, "ci": "other"}), "missing-label"),
        (
            _inventory(labels={**AGENT_LABELS, "image-build": "composition"}),
            "invalid-mapping",
        ),
        (_inventory(executors={"ci": 2}), "incorrect-executor-count"),
        (_inventory(offline={"ci"}), "unavailable-agent"),
    ],
)
def test_agent_preflight_reports_distinct_admission_failures(
    tmp_path: Path,
    inventory: Mapping[str, Any],
    category: str,
) -> None:
    completed = _run_preflight(tmp_path, inventory)

    assert completed.returncode == 1
    assert category in completed.stderr


def test_agent_preflight_accepts_valid_busy_resources(tmp_path: Path) -> None:
    completed = _run_preflight(tmp_path, _inventory(busy={"ci", "cv"}))

    assert completed.returncode == 0, completed.stderr
    assert "busy=ci,cv" in completed.stdout
