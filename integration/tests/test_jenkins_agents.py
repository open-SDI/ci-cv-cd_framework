"""Black-box checks for the repository-owned Jenkins agent deployment."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from ruamel.yaml import YAML

if TYPE_CHECKING:
    from collections.abc import Mapping


REPOSITORY_ROOT = Path(__file__).parents[2]
JENKINS_ROOT = REPOSITORY_ROOT / "deployment" / "jenkins"
AGENT_NAMES = {"integration", "ci", "image-build", "cv", "cd"}
AGENT_LABELS = {
    "integration": "integration",
    "ci": "composition",
    "image-build": "image-build",
    "cv": "cv",
    "cd": "cd",
}
FIXTURE_IMAGE = (
    "docker.io/library/python@"
    "sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2"
)


def _load_yaml(path: Path) -> Mapping[str, Any]:
    loaded = YAML(typ="safe").load(path)  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(loaded, dict)
    return cast("Mapping[str, Any]", loaded)


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

    assert set(services) == {"controller", *AGENT_NAMES}
    assert set(compose["volumes"]) == {"jenkins-home"}
    assert "50000" not in (JENKINS_ROOT / "compose.yaml").read_text()

    controller_networks = set(services["controller"]["networks"])
    assert controller_networks == {
        "controller-egress",
        *(f"controller-{name}" for name in AGENT_NAMES),
    }

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
        assert agent["networks"] == [f"controller-{name}"]
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
        for name in controller_networks
        if name != "controller-egress"
    )
    assert all("docker.sock" not in json.dumps(services[name]) for name in services)


def test_domain_agents_use_their_descriptor_selected_fixture_image() -> None:
    compose = _load_yaml(JENKINS_ROOT / "compose.yaml")
    services = compose["services"]
    descriptor_for_agent = {
        "ci": (
            "composition-fixture-v1.yaml",
            "${JENKINS_COMPOSITION_AGENT_IMAGE}",
        ),
        "image-build": (
            "image-build-fixture-v1.yaml",
            "${JENKINS_IMAGE_BUILD_AGENT_IMAGE}",
        ),
        "cv": ("cv-fixture-v1.yaml", "${JENKINS_CV_AGENT_IMAGE}"),
        "cd": ("cd-fixture-v1.yaml", "${JENKINS_CD_AGENT_IMAGE}"),
    }

    for name, (descriptor_name, image_variable) in descriptor_for_agent.items():
        descriptor = _load_yaml(JENKINS_ROOT / "adapters" / descriptor_name)
        assert descriptor["image"] == FIXTURE_IMAGE
        assert services[name]["build"]["args"]["RUNTIME_IMAGE"] == image_variable

    dockerfile = (JENKINS_ROOT / "agents" / "Dockerfile").read_text()
    assert "FROM ${RUNTIME_IMAGE} AS runtime" in dockerfile
    assert "FROM ${REMOTING_IMAGE} AS remoting" in dockerfile
    assert "FROM runtime\n" in dockerfile
    assert "FROM ${UV_IMAGE} AS uv" in dockerfile
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "/etc/sdi/jenkins-agent-role" in dockerfile
    assert "/etc/sdi/jenkins-agent-label" in dockerfile


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
