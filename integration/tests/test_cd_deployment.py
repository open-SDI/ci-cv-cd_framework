"""CD's decisions without a cluster: gaps, the manifest, and the pass rule.

The pod lists under data/cd-pods are `kubectl get pods -o json` output from a
K3s v1.33.5 stand-in cluster, trimmed of addresses and bookkeeping fields.
"""

from __future__ import annotations

import copy
import json
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from ruamel.yaml import YAML

from sdi_pipeline_integration._cd_deployment import (
    CLUSTER_HOSTS,
    CdInputs,
    Gap,
    Pin,
    deployment_result,
    judge,
    manifest_yaml,
    pin,
    render,
)
from sdi_pipeline_integration._contracts import TargetExecutionProfile
from sdi_pipeline_integration._domain_contracts import (
    CompositionBlueprint,
    DeploymentResult,
    DeploymentSchema,
    ImageBuildResult,
    SourceInputDigest,
)
from sdi_pipeline_integration._yaml_input import parse_yaml

REPOSITORY_ROOT = Path(__file__).parents[2]
CASES = REPOSITORY_ROOT / "integration" / "fixtures" / "cases"
PODS = Path(__file__).parent / "data" / "cd-pods"
SCHEMA = (
    REPOSITORY_ROOT / "integration" / "schemas" / "deployment-result-v1.schema.json"
)

INDEX_DIGEST = f"sha256:{'1' * 64}"
AMD64_DIGEST = f"sha256:{'2' * 64}"
ARM64_DIGESTS = {
    "fixture-localization": f"sha256:{'3' * 64}",
    "fixture-navigation": f"sha256:{'4' * 64}",
    "fixture-mission-control": f"sha256:{'5' * 64}",
}
PLACED = {
    "fixture-localization": "waffle",
    "fixture-navigation": "waffle",
    "fixture-mission-control": "orin",
}
RECORDED_DIGEST = (
    "sha256:d82c2ab94640ded77cf76514ce6a84870761105058a4a9e51b05a8a79be97a6c"
)
UNSCHEDULABLE = (
    "0/4 nodes are available: 4 node(s) didn't match Pod's node affinity/selector. "
    "preemption: 0/4 nodes are available: 4 Preemption is not helpful for scheduling."
)


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _profile(name: str = "waffle-jetson-arm64", **hosts: str) -> TargetExecutionProfile:
    path = REPOSITORY_ROOT / "profiles" / "s-04" / f"{name}.yaml"
    document = parse_yaml(path.read_bytes(), str(path))
    assert isinstance(document, dict)
    for host, architecture in hosts.items():
        document["hosts"][host] = {"role": "edge", "architecture": architecture}
    return TargetExecutionProfile.model_validate(document, strict=True)


def _placement(
    service_id: str, host: str | None, status: str = "resolved"
) -> dict[str, Any]:
    return {
        "service_id": service_id,
        "status": status,
        "artifact_id": f"{service_id}-image",
    } | ({} if host is None else {"host": host})


def _built(service_id: str, *architectures: str) -> dict[str, Any]:
    digests = {"amd64": AMD64_DIGEST, "arm64": ARM64_DIGESTS[service_id]}
    return {
        "service_id": service_id,
        "status": "built",
        "repository": f"sdi/{service_id}",
        "digest": INDEX_DIGEST,
        "platforms": [
            {"architecture": item, "digest": digests[item]}
            for item in architectures or ("amd64", "arm64")
        ],
    }


def _not_built(service_id: str) -> dict[str, Any]:
    return {
        "service_id": service_id,
        "status": "failed",
        "reason": "build-failed",
        "detail": "the build failed",
    }


def _inputs(
    placements: dict[str, dict[str, Any]] | None = None,
    images: dict[str, dict[str, Any]] | None = None,
    profile: TargetExecutionProfile | None = None,
) -> CdInputs:
    deployment = _json(CASES / "composition-s-04-tc-03-c-05" / "deployment-schema.json")
    deployment["placements"] = [
        (placements or {}).get(service_id) or _placement(service_id, host)
        for service_id, host in PLACED.items()
    ]
    image_build = _json(
        CASES / "image-build-s-04-tc-03-c-05" / "image-build-result.json"
    )
    image_build["evidence_basis"] = "implemented"
    image_build["images"] = [
        (images or {}).get(service_id) or _built(service_id) for service_id in PLACED
    ]
    return CdInputs(
        profile or _profile(),
        CompositionBlueprint.model_validate(
            _json(CASES / "composition-s-04-tc-03-c-05" / "composition-blueprint.json"),
            strict=True,
        ),
        DeploymentSchema.model_validate(deployment, strict=True),
        ImageBuildResult.model_validate(image_build, strict=True),
        tuple(
            SourceInputDigest(slot=slot, sha256=f"{index}" * 64)
            for index, slot in enumerate(
                (
                    "target_profile",
                    "composition_blueprint",
                    "deployment_schema",
                    "image_build_result",
                )
            )
        ),
    )


def _matches_schema(result: DeploymentResult) -> None:
    document = json.loads(result.model_dump_json(exclude_none=True))
    Draft202012Validator(_json(SCHEMA)).validate(document)  # pyright: ignore[reportUnknownMemberType]
    assert DeploymentResult.model_validate(document, strict=True) == result


def _reasons(planned: tuple[Pin, ...] | Gap) -> dict[str, str]:
    assert isinstance(planned, Gap)
    return {item.service_id: item.reason for item in planned.placements}


@pytest.mark.parametrize(
    ("placement", "image", "profile", "reason"),
    [
        pytest.param(
            _placement("fixture-navigation", "jetson", "unresolved"),
            _not_built("fixture-navigation"),
            _profile(jetson="arm64"),
            "unresolved-placement",
            id="unresolved-before-image-not-built",
        ),
        pytest.param(
            _placement("fixture-navigation", "jetson"),
            _not_built("fixture-navigation"),
            _profile(jetson="arm64"),
            "image-not-built",
            id="image-not-built-before-no-image-for-architecture",
        ),
        pytest.param(
            _placement("fixture-navigation", "jetson"),
            _built("fixture-navigation", "amd64"),
            _profile(jetson="arm64"),
            "no-image-for-architecture",
            id="no-image-for-architecture-before-host-not-in-cluster",
        ),
        pytest.param(
            _placement("fixture-navigation", "jetson"),
            _built("fixture-navigation"),
            _profile(jetson="arm64"),
            "host-not-in-cluster",
            id="host-not-in-cluster",
        ),
    ],
)
def test_cd_records_the_first_reason_in_order_and_blocks_the_rest(
    placement: dict[str, Any],
    image: dict[str, Any],
    profile: TargetExecutionProfile,
    reason: str,
) -> None:
    inputs = _inputs(
        {"fixture-navigation": placement}, {"fixture-navigation": image}, profile
    )

    planned = pin(inputs)

    assert _reasons(planned) == {
        "fixture-localization": "blocked",
        "fixture-navigation": reason,
        "fixture-mission-control": "blocked",
    }
    result = deployment_result(inputs, _gap(planned))
    assert result.domain_outcome == "not_evaluated"
    assert result.manifest is None
    _matches_schema(result)


def _gap(planned: tuple[Pin, ...] | Gap) -> tuple[Any, ...]:
    assert isinstance(planned, Gap)
    return planned.placements


def test_cd_records_each_gap_and_keeps_an_unresolved_placement_without_a_host() -> None:
    inputs = _inputs(
        {
            "fixture-localization": _placement(
                "fixture-localization", None, "unresolved"
            )
        },
        {
            "fixture-localization": _not_built("fixture-localization"),
            "fixture-mission-control": _built("fixture-mission-control", "amd64"),
        },
    )

    planned = pin(inputs)

    assert [item.model_dump(exclude_none=True) for item in _gap(planned)] == [
        {
            "service_id": "fixture-localization",
            "status": "not_evaluated",
            "reason": "unresolved-placement",
        },
        {
            "service_id": "fixture-navigation",
            "host": "waffle",
            "status": "not_evaluated",
            "reason": "blocked",
        },
        {
            "service_id": "fixture-mission-control",
            "host": "orin",
            "status": "not_evaluated",
            "reason": "no-image-for-architecture",
        },
    ]
    _matches_schema(deployment_result(inputs, _gap(planned)))


@pytest.mark.parametrize(
    "path", sorted((REPOSITORY_ROOT / "profiles" / "s-04").glob("*.yaml"))
)
def test_cd_cluster_has_a_node_for_every_s04_profile_host(path: Path) -> None:
    assert set(_profile(path.stem).hosts) <= set(CLUSTER_HOSTS)


S04_MANIFEST: dict[str, Any] = {
    "apiVersion": "v1",
    "kind": "List",
    "items": [
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": service_id},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"sdi/service": service_id}},
                "template": {
                    "metadata": {"labels": {"sdi/service": service_id}},
                    "spec": {
                        "hostNetwork": True,
                        "nodeSelector": {"sdi/host": host},
                        "containers": [
                            {
                                "name": service_id,
                                "image": (
                                    f"registry:5000/sdi/{service_id}"
                                    f"@{ARM64_DIGESTS[service_id]}"
                                ),
                            }
                        ],
                    },
                },
            },
        }
        for service_id, host in PLACED.items()
    ],
}


def test_cd_renders_an_s04_combination_with_arm64_hosts_to_per_platform_digests() -> (
    None
):
    planned = pin(_inputs())

    assert not isinstance(planned, Gap)
    assert render(planned) == S04_MANIFEST


def test_cd_pins_each_host_to_the_digest_for_its_own_architecture() -> None:
    inputs = _inputs(
        {"fixture-mission-control": _placement("fixture-mission-control", "xycar")},
        profile=_profile("waffle-xycar-amd64"),
    )

    planned = pin(inputs)

    assert not isinstance(planned, Gap)
    assert {item.service_id: item.digest for item in planned} == {
        "fixture-localization": ARM64_DIGESTS["fixture-localization"],
        "fixture-navigation": ARM64_DIGESTS["fixture-navigation"],
        "fixture-mission-control": AMD64_DIGEST,
    }


def _load(text: str, version: tuple[int, int] | None = None) -> object:
    yaml = YAML(typ="safe", pure=True)
    yaml.version = version
    return yaml.load(StringIO(text))  # pyright: ignore[reportUnknownMemberType]


def test_cd_manifest_yaml_reads_back_as_the_same_list() -> None:
    text = manifest_yaml(S04_MANIFEST).decode()

    assert _load(text) == S04_MANIFEST
    assert _load(text, (1, 1)) == S04_MANIFEST
    assert manifest_yaml(copy.deepcopy(S04_MANIFEST)).decode() == text


def test_cd_manifest_yaml_keeps_each_image_reference_on_its_own_line() -> None:
    text = manifest_yaml(S04_MANIFEST).decode()

    for item in S04_MANIFEST["items"]:
        image = item["spec"]["template"]["spec"]["containers"][0]["image"]
        assert f"  image: {image}\n" in text


def test_cd_manifest_yaml_quotes_words_kubectl_reads_as_booleans() -> None:
    manifest = render([Pin("yes", "on", f"registry:5000/sdi/yes@{AMD64_DIGEST}")])

    text = manifest_yaml(manifest).decode()

    assert text.startswith("apiVersion: v1\n")
    assert _load(text, (1, 1)) == manifest


def _pods(*pods: dict[str, Any]) -> str:
    return json.dumps({"apiVersion": "v1", "kind": "List", "items": list(pods)})


def _recorded(file: str, name: str, service_id: str) -> dict[str, Any]:
    pod = next(
        item for item in _json(PODS / file)["items"] if item["metadata"]["name"] == name
    )
    pod["metadata"]["labels"] = {"sdi/service": service_id}
    return pod


def _running(service_id: str = "fixture-localization") -> dict[str, Any]:
    return _recorded("arm64-running.json", "probe-66d4d75bcb-n5wlp", service_id)


def _pinned(service_id: str, host: str, digest: str = RECORDED_DIGEST) -> Pin:
    return Pin(service_id, host, f"registry:5000/sdi/{service_id}@{digest}")


def _crash_looping() -> dict[str, Any]:
    pod = _recorded("failmodes.json", "crash", "fixture-localization")
    pod["status"]["containerStatuses"][0]["state"] = {
        "waiting": {
            "reason": "CrashLoopBackOff",
            "message": (
                "back-off 40s restarting failed container=c "
                "pod=crash_sdi-failmodes(5f0c1d2e-0d4b-4a8e-9a43-1d6f3e2b7c10)"
            ),
        }
    }
    return pod


def _unscheduled() -> dict[str, Any]:
    return {
        "metadata": {
            "name": "fixture-localization-7d9c",
            "labels": {"sdi/service": "fixture-localization"},
        },
        "spec": {"nodeSelector": {"sdi/host": "waffle"}},
        "status": {
            "phase": "Pending",
            "conditions": [
                {
                    "type": "PodScheduled",
                    "status": "False",
                    "reason": "Unschedulable",
                    "message": UNSCHEDULABLE,
                }
            ],
        },
    }


def _running_with(phase: str | None = None, **container: object) -> dict[str, Any]:
    pod = _running()
    if phase is not None:
        pod["status"]["phase"] = phase
    pod["status"]["containerStatuses"][0] |= container
    return pod


def test_cd_passes_a_pod_running_its_pinned_digest_on_its_host() -> None:
    pinned = _pinned("fixture-localization", "waffle")

    (record,) = judge([pinned], _pods(_running()))

    assert record.model_dump(exclude_none=True) == {
        "service_id": "fixture-localization",
        "host": "waffle",
        "status": "deployed",
        "node": "waffle",
        "image": pinned.image,
        "image_id": f"registry:5000/sdi/probe@{RECORDED_DIGEST}",
        "phase": "Running",
        "restart_count": 0,
    }


@pytest.mark.parametrize(
    ("pinned", "pod", "observed", "detail"),
    [
        pytest.param(
            _pinned("fixture-localization", "burger", f"sha256:{'0' * 64}"),
            _recorded("failmodes.json", "badpull", "fixture-localization"),
            {"node": "burger", "phase": "Pending", "restart_count": 0},
            "ImagePullBackOff: Back-off pulling image registry:5000/sdi/probe@",
            id="image-pull-back-off",
        ),
        pytest.param(
            _pinned("fixture-localization", "waffle"),
            _crash_looping(),
            {
                "node": "waffle",
                "phase": "Running",
                "restart_count": 3,
                "image_id": f"registry:5000/sdi/probe@{RECORDED_DIGEST}",
            },
            "CrashLoopBackOff: back-off 40s restarting failed container=c",
            id="crash-loop-back-off",
        ),
        pytest.param(
            _pinned("fixture-localization", "waffle"),
            _recorded("failmodes.json", "crash", "fixture-localization"),
            {
                "node": "waffle",
                "phase": "Running",
                "restart_count": 3,
                "image_id": f"registry:5000/sdi/probe@{RECORDED_DIGEST}",
            },
            "Error (exit 1); restarted 3 times, last Error (exit 1)",
            id="crash-between-restarts",
        ),
        pytest.param(
            _pinned("fixture-localization", "waffle"),
            _unscheduled(),
            {"phase": "Pending"},
            f"Unschedulable: {UNSCHEDULABLE}",
            id="pending-without-a-node",
        ),
        pytest.param(
            _pinned("fixture-localization", "orin"),
            _running(),
            {
                "node": "waffle",
                "phase": "Running",
                "restart_count": 0,
                "image_id": f"registry:5000/sdi/probe@{RECORDED_DIGEST}",
            },
            "on node waffle, not orin",
            id="wrong-node",
        ),
        pytest.param(
            _pinned(
                "fixture-localization", "waffle", ARM64_DIGESTS["fixture-localization"]
            ),
            _running(),
            {
                "node": "waffle",
                "phase": "Running",
                "restart_count": 0,
                "image_id": f"registry:5000/sdi/probe@{RECORDED_DIGEST}",
            },
            f"ran {RECORDED_DIGEST}, not the pinned "
            f"{ARM64_DIGESTS['fixture-localization']}",
            id="wrong-digest",
        ),
        pytest.param(
            _pinned("fixture-localization", "waffle"),
            _running_with(
                restartCount=1,
                lastState={"terminated": {"reason": "Error", "exitCode": 137}},
            ),
            {
                "node": "waffle",
                "phase": "Running",
                "restart_count": 1,
                "image_id": f"registry:5000/sdi/probe@{RECORDED_DIGEST}",
            },
            "restarted 1 times, last Error (exit 137)",
            id="restarted",
        ),
        pytest.param(
            _pinned("fixture-localization", "waffle"),
            None,
            {},
            "no pod was created",
            id="no-pod",
        ),
        pytest.param(
            _pinned("fixture-localization", "waffle"),
            _running_with("Unknown"),
            {
                "node": "waffle",
                "phase": "Unknown",
                "restart_count": 0,
                "image_id": f"registry:5000/sdi/probe@{RECORDED_DIGEST}",
            },
            "phase Unknown",
            id="running-container-in-a-pod-not-running",
        ),
        pytest.param(
            _pinned("fixture-localization", "waffle"),
            _running_with(state={}),
            {
                "node": "waffle",
                "phase": "Running",
                "restart_count": 0,
                "image_id": f"registry:5000/sdi/probe@{RECORDED_DIGEST}",
            },
            "the container is not running",
            id="container-without-a-state",
        ),
        pytest.param(
            _pinned("fixture-localization", "waffle"),
            _running_with(imageID=""),
            {"node": "waffle", "phase": "Running", "restart_count": 0},
            "the image ID is not a digest reference",
            id="empty-image-id",
        ),
        pytest.param(
            _pinned("fixture-localization", "waffle"),
            _running_with(imageID="registry:5000/sdi/probe:latest"),
            {"node": "waffle", "phase": "Running", "restart_count": 0},
            "the image ID is not a digest reference",
            id="unparseable-image-id",
        ),
    ],
)
def test_cd_fails_a_pod_that_breaks_the_pass_rule_with_its_reason(
    pinned: Pin, pod: dict[str, Any] | None, observed: dict[str, Any], detail: str
) -> None:
    (record,) = judge([pinned], _pods() if pod is None else _pods(pod))

    dumped = record.model_dump(exclude_none=True)
    assert dumped.pop("detail").startswith(detail)
    assert (
        dumped
        == {
            "service_id": "fixture-localization",
            "host": pinned.host,
            "status": "failed",
            "image": pinned.image,
        }
        | observed
    )


def test_cd_cleans_a_kubernetes_message_into_a_detail() -> None:
    (record,) = judge(
        [_pinned("fixture-localization", "burger", f"sha256:{'0' * 64}")],
        _pods(_recorded("failmodes.json", "badpull", "fixture-localization")),
    )

    assert record.status == "failed"
    assert record.detail.isascii()
    assert len(record.detail) <= 256
    assert '"' not in record.detail
    assert "\\" not in record.detail


def test_cd_assembles_a_result_from_judged_pods_that_matches_the_schema() -> None:
    inputs = _inputs(
        images={
            service_id: _built(service_id)
            | {"platforms": [{"architecture": "arm64", "digest": RECORDED_DIGEST}]}
            for service_id in PLACED
        }
    )
    planned = pin(inputs)
    assert not isinstance(planned, Gap)
    pods: list[dict[str, Any]] = [
        _running("fixture-localization"),
        _crash_looping()
        | {"metadata": {"labels": {"sdi/service": "fixture-navigation"}}},
        _running("fixture-mission-control"),
    ]
    pods[2]["spec"]["nodeName"] = "orin"

    result = deployment_result(inputs, judge(planned, _pods(*pods)), render(planned))

    assert result.domain_outcome == "failed"
    assert [item.status for item in result.placements] == [
        "deployed",
        "failed",
        "deployed",
    ]
    assert result.manifest == render(planned)
    _matches_schema(result)
