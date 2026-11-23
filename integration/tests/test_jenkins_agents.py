"""Black-box checks for the repository-owned Jenkins agent deployment."""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from ruamel.yaml import YAML

from sdi_pipeline_integration._cd_deployment import CLUSTER_HOSTS, HOST_LABEL
from sdi_pipeline_integration._integration_code import BAKED_ROOTS
from sdi_pipeline_integration._stage_registry import AGENTS, STAGES

if TYPE_CHECKING:
    from collections.abc import Mapping


REPOSITORY_ROOT = Path(__file__).parents[2]
JENKINS_ROOT = REPOSITORY_ROOT / "deployment" / "jenkins"
AGENT_NAMES = {agent.node for agent in AGENTS}
BUILDER_SERVICES = {"buildkitd", "registry"}
CV_ENGINE = "cv-engine"
K3S_SERVER = "k3s-xycar"
K3S_NODES = {f"k3s-{host}" for host in CLUSTER_HOSTS}
RUNNER_STACK_SERVICES = {
    "runner-upstream",
    "runner-fork",
    "runner-upstream-proxy",
    "runner-fork-proxy",
}
AGENT_LABELS = {agent.node: agent.label for agent in AGENTS}
AGENT_TARGETS = dict.fromkeys(AGENT_NAMES, "agent") | {
    "image-build": "image-build",
    "cd": "cd",
}
AGENT_NETWORKS = {name: [f"controller-{name}"] for name in AGENT_NAMES} | {
    "image-build": ["controller-image-build", "image-build-builder"],
    "cv": ["controller-cv", "cv-engine-agent"],
    "cd": ["controller-cd", "k3s-cd"],
}
AGENT_VOLUMES = {"cd": ["k3s-kubeconfig:/etc/sdi/k3s:ro"]}
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
        CV_ENGINE,
        *K3S_NODES,
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
        assert agent.get("volumes", []) == AGENT_VOLUMES.get(name, [])
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


def test_no_service_mounts_the_host_docker_socket() -> None:
    services = _load_yaml(JENKINS_ROOT / "compose.yaml")["services"]

    assert all(
        "docker.sock" not in json.dumps(service.get("volumes", []))
        for service in services.values()
    )


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
    assert "FROM ${KUBECTL_IMAGE} AS kubectl" in dockerfile
    assert dockerfile.endswith(
        "FROM agent AS image-build\n"
        "COPY --from=buildkit /usr/bin/buildctl /usr/bin/buildctl\n"
        "\n"
        "FROM agent AS cd\n"
        "COPY --from=kubectl /bin/kubectl /usr/bin/kubectl\n"
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
    assert registry["networks"] == [
        "image-build-builder",
        "cv-engine-registry",
        "k3s-registry",
    ]
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


def test_cv_engine_is_reachable_only_from_the_cv_agent() -> None:
    compose = _load_yaml(JENKINS_ROOT / "compose.yaml")
    services = compose["services"]
    networks = compose["networks"]
    engine = services[CV_ENGINE]

    assert engine["image"] == _installation_pins()["SDI_CV_ENGINE_IMAGE"]
    assert engine["platform"] == "linux/amd64"
    assert engine["restart"] == "no"
    assert "ports" not in engine
    assert "build" not in engine
    assert engine["privileged"] is True
    assert engine["environment"] == {"DOCKER_TLS_CERTDIR": ""}
    assert engine["command"] == [
        "dockerd",
        "-H",
        "unix:///var/run/docker.sock",
        "-H",
        "tcp://0.0.0.0:2375",
        "--tls=false",
        "--insecure-registry",
        "registry:5000",
    ]
    # The image declares /var/lib/docker as VOLUME.
    assert engine["volumes"] == ["cv-engine-state:/var/lib/docker"]
    assert engine["healthcheck"]["test"][0] == "CMD"
    assert engine["networks"] == [
        "cv-engine-agent",
        "cv-engine-registry",
        "cv-engine-egress",
    ]

    assert networks["cv-engine-agent"]["internal"] is True
    assert networks["cv-engine-registry"]["internal"] is True
    assert networks["cv-engine-egress"] is None
    members = {
        name: set(service.get("networks", [])) for name, service in services.items()
    }
    assert {name for name, nets in members.items() if "cv-engine-agent" in nets} == {
        "cv",
        CV_ENGINE,
    }
    assert {name for name, nets in members.items() if "cv-engine-egress" in nets} == {
        CV_ENGINE
    }
    assert {name for name, nets in members.items() if "cv-engine-registry" in nets} == {
        CV_ENGINE,
        "registry",
    }


def test_cluster_hosts_are_the_compose_k3s_node_hosts() -> None:
    services = _load_yaml(JENKINS_ROOT / "compose.yaml")["services"]
    host_flag = f"--node-label={HOST_LABEL}="

    compose_hosts = sorted(
        argument.removeprefix(host_flag)
        for service in services.values()
        if "/rancher/k3s:" in service.get("image", "")
        for argument in service["command"]
        if argument.startswith(host_flag)
    )

    assert compose_hosts == sorted(CLUSTER_HOSTS)


def test_k3s_nodes_reach_only_the_registry_and_their_api_only_the_cd_agent() -> None:
    compose = _load_yaml(JENKINS_ROOT / "compose.yaml")
    services = compose["services"]
    networks = compose["networks"]
    server = services[K3S_SERVER]
    token = server["environment"]["K3S_TOKEN"]

    for name in K3S_NODES:
        node = services[name]
        host = name.removeprefix("k3s-")
        assert node["image"] == _installation_pins()["SDI_K3S_IMAGE"]
        assert node["platform"] == "linux/amd64"
        assert node["restart"] == "no"
        assert node["privileged"] is True
        assert node["tmpfs"] == ["/run", "/var/run"]
        assert "ports" not in node
        assert "build" not in node
        assert node["command"][1:3] == [
            f"--node-name={host}",
            f"--node-label=sdi/host={host}",
        ]
        assert node["environment"]["K3S_TOKEN"] == token
        assert node["volumes"][0] == (
            "./k3s/registries.yaml:/etc/rancher/k3s/registries.yaml:ro"
        )
    assert server["command"] == [
        "server",
        "--node-name=xycar",
        "--node-label=sdi/host=xycar",
        "--disable=traefik,servicelb,metrics-server,local-storage",
        f"--tls-san={K3S_SERVER}",
    ]
    assert server["environment"]["K3S_KUBECONFIG_OUTPUT"] == (
        "/etc/sdi/k3s/kubeconfig.yaml"
    )
    assert server["environment"]["K3S_KUBECONFIG_MODE"] == "644"
    for name in K3S_NODES - {K3S_SERVER}:
        assert services[name]["command"][0] == "agent"
        assert services[name]["environment"]["K3S_URL"] == f"https://{K3S_SERVER}:6443"

    mirror = _load_yaml(JENKINS_ROOT / "k3s" / "registries.yaml")
    assert mirror == {
        "mirrors": {"registry:5000": {"endpoint": ["http://registry:5000"]}}
    }

    assert networks["k3s-cd"]["internal"] is True
    assert networks["k3s-registry"]["internal"] is True
    assert networks["k3s-egress"] is None
    members = {
        name: set(service.get("networks", [])) for name, service in services.items()
    }
    assert {name for name, nets in members.items() if "k3s-cd" in nets} == {
        "cd",
        K3S_SERVER,
    }
    assert {name for name, nets in members.items() if "k3s-registry" in nets} == {
        *K3S_NODES,
        "registry",
    }
    assert {name for name, nets in members.items() if "k3s-egress" in nets} == (
        K3S_NODES
    )
    kubeconfig_mounts = {
        name: volume
        for name, service in services.items()
        for volume in service.get("volumes", [])
        if volume.startswith("k3s-kubeconfig:")
    }
    assert kubeconfig_mounts == {
        K3S_SERVER: "k3s-kubeconfig:/etc/sdi/k3s",
        "cd": "k3s-kubeconfig:/etc/sdi/k3s:ro",
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
