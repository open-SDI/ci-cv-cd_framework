"""The CD adapter against the session's K3s stand-in cluster.

Each test pushes its own images to the session registry, runs the adapter from
the host with the host kubectl, and leaves its request, evidence, diagnostic,
and applied YAML under its temporary directory.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import uuid
from functools import partial
from typing import TYPE_CHECKING, Any, Self, cast

import pytest
import requests

from sdi_pipeline_integration import _cd_adapter
from sdi_pipeline_integration._adapter_kit import DomainAdapter, serve
from sdi_pipeline_integration._cd_adapter import Cluster, deploy
from sdi_pipeline_integration._cd_deployment import manifest_yaml
from sdi_pipeline_integration._domain_contracts import (
    DeployedPlacement,
    FailedPlacement,
    image_digest,
)

from ._cd_cluster import SessionCluster, session_cluster
from ._cd_stage import CdRun, CdStage, built

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

pytestmark = pytest.mark.k3s

# Not pause 3.6: K3s v1.33.5 already holds it as its sandbox image, and a node
# then reports that image's index digest as the pod's imageID.
RUNS_UNTIL_STOPPED = (
    "docker.io/rancher/mirrored-pause:3.10",
    {
        "amd64": "7c38f24774e3cbd906d2d33c38354ccf787635581c122965132c9bd309754d4a",
        "arm64": "e50b7059b633caf3c1449b8da680d11845cda4506b513ee7a2de00725f0a34a7",
    },
)
EXITS_AT_ONCE = (
    "docker.io/library/busybox:1.37.0",
    {
        "amd64": "66a6306db78bf2dbf3487f293aa8d6990d8e506fdffab9cc43fe422becf886e4",
        "arm64": "d82c2ab94640ded77cf76514ce6a84870761105058a4a9e51b05a8a79be97a6c",
    },
)
LAST_TERMINATION_SUFFIX = re.compile(r"restarted \d+ times, last Completed \(exit 0\)$")
ABSENT = f"sha256:{'0' * 64}"
INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
ARCHITECTURES = {"waffle": "arm64", "xycar": "amd64"}
NAMESPACE_GONE_SECONDS = 180


@pytest.fixture(scope="session")
def cluster() -> SessionCluster:
    return session_cluster()


def _push(
    cluster: SessionCluster, service_id: str, source: tuple[str, Mapping[str, str]]
) -> dict[str, str]:
    registry = cluster.registry_url.removeprefix("http://")
    subprocess.run(
        [
            "docker",
            "buildx",
            "imagetools",
            "create",
            "--tag",
            f"{registry}/sdi/{service_id}:k3s",
            *(f"{source[0]}@sha256:{digest}" for digest in source[1].values()),
        ],
        check=True,
        capture_output=True,
        timeout=300,
    )
    response = requests.get(
        f"{cluster.registry_url}/v2/sdi/{service_id}/manifests/k3s",
        headers={"Accept": ", ".join(INDEX_TYPES)},
        timeout=30,
    )
    response.raise_for_status()
    assert response.headers["Content-Type"] in INDEX_TYPES
    manifests = cast("list[dict[str, Any]]", response.json()["manifests"])
    platforms = {
        str(item["platform"]["architecture"]): str(item["digest"]) for item in manifests
    }
    assert platforms == {name: f"sha256:{digest}" for name, digest in source[1].items()}
    return platforms


def _adapter(cluster: SessionCluster, **timing: float) -> DomainAdapter:
    return serve(
        "cd",
        partial(deploy, cluster=Cluster(cluster.server, cluster.kubeconfig, **timing)),
    )


def _stage(tmp_path: Path) -> CdStage:
    return CdStage(tmp_path / "stage", str(uuid.uuid4()))


def _keep(tmp_path: Path, run: CdRun) -> None:
    if run.evidence is not None and run.evidence.manifest is not None:
        (tmp_path / "applied.yaml").write_bytes(manifest_yaml(run.evidence.manifest))


def _assert_namespace_gone(cluster: SessionCluster, namespace: str) -> None:
    deadline = time.monotonic() + NAMESPACE_GONE_SECONDS
    while True:
        read = cluster.kubectl("get", "namespace", namespace, "-o", "name")
        if read.returncode != 0 and "NotFound" in read.stderr:
            return
        assert time.monotonic() < deadline, f"{namespace} is still there: {read.stdout}"
        time.sleep(5)


def _assert_detail_is_clean(detail: str) -> None:
    assert detail.isascii()
    assert len(detail) <= 256
    assert '"' not in detail
    assert "\\" not in detail


class _Observer:
    def __init__(self, cluster: SessionCluster, namespace: str) -> None:
        self._cluster = cluster
        self._namespace = namespace
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch)
        self.pods: dict[str, Any] | None = None
        self.deployments: dict[str, Any] | None = None

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_exception: object) -> None:
        self._stop.set()
        self._thread.join()

    def _read(self, kind: str) -> dict[str, Any] | None:
        read = self._cluster.kubectl("get", kind, "-n", self._namespace, "-o", "json")
        return (
            cast("dict[str, Any]", json.loads(read.stdout))
            if read.returncode == 0
            else None
        )

    def _watch(self) -> None:
        while not self._stop.wait(2):
            pods = self._read("pods")
            if (
                pods
                and pods["items"]
                and all(
                    item["status"].get("phase") == "Running"
                    and item["status"].get("containerStatuses")
                    for item in pods["items"]
                )
            ):
                self.pods = pods
                self.deployments = self.deployments or self._read("deployments")


@pytest.fixture
def unlabelled_orin(cluster: SessionCluster) -> Iterator[None]:
    assert cluster.kubectl("label", "node", "orin", "sdi/host-").returncode == 0
    try:
        yield
    finally:
        relabel = cluster.kubectl("label", "node", "orin", "sdi/host=orin")
        assert relabel.returncode == 0, relabel.stderr


def test_cd_a_composition_with_an_arm64_host_deploys_and_passes(
    cluster: SessionCluster, tmp_path: Path
) -> None:
    placed = {"k3s-pass-waffle": "waffle", "k3s-pass-xycar": "xycar"}
    platforms = {
        service_id: _push(cluster, service_id, RUNS_UNTIL_STOPPED)
        for service_id in placed
    }
    stage = _stage(tmp_path)
    stage.write(
        placed,
        [built(service_id, platforms[service_id]) for service_id in placed],
        profile="waffle-xycar-amd64",
    )

    with _Observer(cluster, stage.namespace) as observer:
        run = stage.run(_adapter(cluster))
    _keep(tmp_path, run)

    assert run.response.execution_conclusion == "succeeded"
    evidence = run.evidence
    assert evidence is not None
    assert (evidence.domain_outcome, evidence.cluster_kind) == ("succeeded", "stand_in")
    observed = {
        item["metadata"]["labels"]["sdi/service"]: item
        for item in (observer.pods or {"items": []})["items"]
    }
    (tmp_path / "pods.json").write_text(json.dumps(observer.pods, indent=2))
    for record in evidence.placements:
        assert isinstance(record, DeployedPlacement)
        pinned = platforms[record.service_id][ARCHITECTURES[record.host]]
        assert (record.node, record.phase, record.restart_count) == (
            record.host,
            "Running",
            0,
        )
        assert image_digest(record.image) == image_digest(record.image_id) == pinned
        pod = observed[record.service_id]
        status = pod["status"]["containerStatuses"][0]
        assert pod["spec"]["nodeName"] == record.node
        assert image_digest(status["imageID"]) == pinned
        assert status["restartCount"] == 0
        assert f"{record.service_id}: Running after" in run.diagnostic
    applied = {
        item["metadata"]["name"]: json.loads(
            item["metadata"]["annotations"][
                "kubectl.kubernetes.io/last-applied-configuration"
            ]
        )
        for item in (observer.deployments or {"items": []})["items"]
    }
    manifest = cast("dict[str, list[dict[str, Any]]]", evidence.manifest)
    for item in manifest["items"]:
        name = item["metadata"]["name"]
        # kubectl records an empty annotations map, and may record the namespace.
        metadata = applied[name]["metadata"]
        metadata.pop("namespace", None)
        assert metadata.pop("annotations") == {}
        assert applied[name] == item
    _assert_namespace_gone(cluster, stage.namespace)


def test_cd_a_bad_digest_fails_with_an_image_pull_detail(
    cluster: SessionCluster, tmp_path: Path
) -> None:
    stage = _stage(tmp_path)
    stage.write(
        {"k3s-bad-digest": "xycar"},
        [built("k3s-bad-digest", {"amd64": ABSENT, "arm64": ABSENT})],
        profile="waffle-xycar-amd64",
    )

    run = stage.run(_adapter(cluster))
    _keep(tmp_path, run)

    assert run.response.execution_conclusion == "succeeded"
    assert run.evidence is not None
    assert run.evidence.domain_outcome == "failed"
    (record,) = run.evidence.placements
    assert isinstance(record, FailedPlacement)
    assert re.search(r"ImagePullBackOff|ErrImagePull", record.detail), record.detail
    _assert_detail_is_clean(record.detail)
    _assert_namespace_gone(cluster, stage.namespace)


def test_cd_a_crashing_image_fails_with_its_last_termination_reason(
    cluster: SessionCluster, tmp_path: Path
) -> None:
    platforms = _push(cluster, "k3s-crash", EXITS_AT_ONCE)
    stage = _stage(tmp_path)
    stage.write(
        {"k3s-crash": "xycar"},
        [built("k3s-crash", platforms)],
        profile="waffle-xycar-amd64",
    )

    run = stage.run(_adapter(cluster))
    _keep(tmp_path, run)

    assert run.response.execution_conclusion == "succeeded"
    assert run.evidence is not None
    assert run.evidence.domain_outcome == "failed"
    (record,) = run.evidence.placements
    assert isinstance(record, FailedPlacement)
    assert record.restart_count is not None
    assert record.restart_count > 0
    assert LAST_TERMINATION_SUFFIX.search(record.detail), record.detail
    _assert_detail_is_clean(record.detail)
    _assert_namespace_gone(cluster, stage.namespace)


def test_cd_the_namespace_is_deleted_after_an_exception_mid_run(
    cluster: SessionCluster, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    platforms = _push(cluster, "k3s-exception", RUNS_UNTIL_STOPPED)
    stage = _stage(tmp_path)
    stage.write(
        {"k3s-exception": "xycar"},
        [built("k3s-exception", platforms)],
        profile="waffle-xycar-amd64",
    )
    existed: list[bool] = []

    def judge_fails(*_arguments: object) -> None:
        read = cluster.kubectl("get", "namespace", stage.namespace, "-o", "name")
        existed.append(read.returncode == 0)
        message = "the pass rule broke mid-run"
        raise RuntimeError(message)

    monkeypatch.setattr(_cd_adapter, "judge", judge_fails)

    with pytest.raises(RuntimeError, match="the pass rule broke mid-run"):
        _adapter(cluster)(stage.argv())

    assert existed == [True]
    _assert_namespace_gone(cluster, stage.namespace)


@pytest.mark.usefixtures("unlabelled_orin")
def test_cd_a_node_that_never_becomes_ready_fails_execution_without_evidence(
    cluster: SessionCluster, tmp_path: Path
) -> None:
    platforms = _push(cluster, "k3s-orin", RUNS_UNTIL_STOPPED)
    stage = _stage(tmp_path)
    stage.write(
        {"k3s-orin": "orin"},
        [built("k3s-orin", platforms)],
        profile="waffle-jetson-arm64",
    )

    run = stage.run(_adapter(cluster, node_wait_seconds=10))

    assert run.response.execution_conclusion == "failed"
    assert run.response.reason is not None
    assert run.response.reason.code == "sdi.cd.nodes-not-ready"
    assert run.evidence is None
    assert not (stage.output_root / "outputs").exists()
    assert "no Ready node for every placed host" in run.diagnostic
    never = cluster.kubectl("get", "namespace", stage.namespace, "-o", "name")
    assert never.returncode != 0
    assert "NotFound" in never.stderr
