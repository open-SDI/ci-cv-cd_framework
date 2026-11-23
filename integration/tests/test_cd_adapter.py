"""The CD adapter's run sequence against a kubectl that replays recorded state.

The fake kubectl logs each call and answers `get nodes` and `get pods` with
JSON shaped like a K3s v1.33.5 reply. It proves the calls the adapter makes and
the bounds it keeps, not that it drives a cluster; the k3s tier proves that.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from sdi_pipeline_integration import _cd_adapter
from sdi_pipeline_integration._adapter_kit import serve
from sdi_pipeline_integration._cd_adapter import (
    KUBECTL_TIMEOUT_SECONDS,
    STAND_IN,
    TEARDOWN_MARGIN_SECONDS,
    WORK_LIMIT_DETAIL,
    Cluster,
    deploy,
)
from sdi_pipeline_integration._cd_deployment import CLUSTER_HOSTS, manifest_yaml
from sdi_pipeline_integration._stage_registry import STAGES

from ._cd_stage import CdStage, built

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

SERVER = "https://k3s-xycar:6443"
AMD64 = f"sha256:{'a' * 64}"
ARM64 = f"sha256:{'b' * 64}"
PLACED = {"cd-waffle": "waffle", "cd-xycar": "xycar"}
DIGESTS = {"cd-waffle": ARM64, "cd-xycar": AMD64}
FAKE_KUBECTL = """#!{python}
import json, os, sys, time
from pathlib import Path

state = Path({state!r})
arguments = sys.argv[1:]
call = {{"argv": arguments, "environment": sorted(os.environ)}}
call["stdin"] = sys.stdin.read()
with (state / "calls.jsonl").open("a") as log:
    log.write(json.dumps(call) + "\\n")
verb = arguments[7]
if (state / f"{{verb}}.fails").exists():
    sys.stderr.write("error: the fake cluster refused\\n")
    sys.exit(1)
kinds = [kind for kind in ("nodes", "pods") if kind in arguments]
for name in [verb, *kinds]:
    count = state / f"{{name}}.count"
    calls = int(count.read_text()) + 1 if count.exists() else 1
    count.write_text(str(calls))
    if (state / f"{{name}}.slow").exists():
        seconds, first = (state / f"{{name}}.slow").read_text().split()
        if calls >= int(first):
            time.sleep(float(seconds))
for kind in kinds:
    sys.stdout.write((state / f"{{kind}}.json").read_text())
"""


def _nodes(ready: Collection[str]) -> dict[str, Any]:
    return {
        "items": [
            {
                "metadata": {"name": host, "labels": {"sdi/host": host}},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            }
            for host in ready
        ]
    }


def _pods(waiting: Mapping[str, str] | None = None) -> dict[str, Any]:
    waiting = waiting or {}
    return {
        "items": [
            {
                "metadata": {"labels": {"sdi/service": service_id}},
                "spec": {"nodeName": host},
                "status": _waiting(waiting[service_id])
                if service_id in waiting
                else _running(service_id),
            }
            for service_id, host in PLACED.items()
        ]
    }


def _running(service_id: str) -> dict[str, Any]:
    return {
        "phase": "Running",
        "containerStatuses": [
            {
                "imageID": f"registry:5000/sdi/{service_id}@{DIGESTS[service_id]}",
                "restartCount": 0,
                "state": {"running": {"startedAt": "2026-10-24T00:00:00Z"}},
                "lastState": {},
            }
        ],
    }


def _waiting(reason: str) -> dict[str, Any]:
    return {
        "phase": "Pending",
        "containerStatuses": [
            {
                "imageID": "",
                "restartCount": 0,
                "state": {"waiting": {"reason": reason}},
                "lastState": {},
            }
        ],
    }


class _FakeCluster:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        root.mkdir()
        binary = root / "bin" / "kubectl"
        binary.parent.mkdir()
        binary.write_text(FAKE_KUBECTL.format(python=sys.executable, state=str(root)))
        binary.chmod(0o755)
        monkeypatch.setenv("PATH", f"{binary.parent}:/usr/bin:/bin")
        self.answer(nodes=_nodes(CLUSTER_HOSTS), pods=_pods())

    def answer(self, **replies: dict[str, Any]) -> None:
        for kind, reply in replies.items():
            (self.root / f"{kind}.json").write_text(json.dumps(reply))

    def refuse(self, verb: str) -> None:
        (self.root / f"{verb}.fails").touch()

    def slow(self, name: str, seconds: float, *, from_call: int = 1) -> None:
        (self.root / f"{name}.slow").write_text(f"{seconds} {from_call}")

    def cluster(self, **timing: float) -> Cluster:
        return Cluster(SERVER, self.root / "kubeconfig.yaml", **timing)

    def calls(self) -> list[dict[str, Any]]:
        log = self.root / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeCluster:
    return _FakeCluster(tmp_path / "fake", monkeypatch)


def _stage(tmp_path: Path, **request: int) -> CdStage:
    stage = CdStage(tmp_path / "stage", str(uuid.uuid4()))
    stage.write(
        PLACED,
        [built(service_id, {"amd64": AMD64, "arm64": ARM64}) for service_id in PLACED],
        profile="waffle-xycar-amd64",
        **request,
    )
    return stage


def test_cd_every_kubectl_call_names_the_cluster_and_a_cache_directory(
    tmp_path: Path, fake: _FakeCluster
) -> None:
    stage = _stage(tmp_path)

    run = stage.run(
        serve("cd", partial(deploy, cluster=fake.cluster(settle_seconds=0)))
    )

    assert run.evidence is not None
    assert run.evidence.domain_outcome == "succeeded"
    calls = fake.calls()
    assert [call["argv"][7] for call in calls] == [
        "get",
        "create",
        "apply",
        "get",
        "get",
        "delete",
    ]
    for call in calls:
        argv = call["argv"]
        assert argv[:4] == [
            "--kubeconfig",
            str(fake.root / "kubeconfig.yaml"),
            "--server",
            SERVER,
        ]
        assert argv[4] == "--cache-dir"
        assert Path(argv[5]).is_absolute()
        assert call["environment"] == ["HOME", "LC_ALL", "PATH"]
    assert len({call["argv"][5] for call in calls}) == 1
    apply = calls[2]
    assert apply["argv"][8:] == ["-n", stage.namespace, "-f", "-"]
    assert apply["stdin"].encode() == manifest_yaml(run.evidence.manifest or {})
    assert calls[-1]["argv"][8:] == [
        "namespace",
        stage.namespace,
        "--wait=false",
        "--ignore-not-found",
    ]


@pytest.mark.parametrize(
    ("slack", "outcome"), [(3, "succeeded"), (-3, "failed")], ids=["fits", "cut"]
)
def test_cd_the_adapter_deadline_is_the_invocation_deadline_less_the_margin(
    tmp_path: Path, fake: _FakeCluster, slack: int, outcome: str
) -> None:
    settle = 5
    stage = _stage(
        tmp_path, work_limit_seconds=TEARDOWN_MARGIN_SECONDS + settle + slack
    )

    run = stage.run(
        serve("cd", partial(deploy, cluster=fake.cluster(settle_seconds=settle)))
    )

    assert run.response.execution_conclusion == "succeeded"
    assert run.evidence is not None
    assert run.evidence.domain_outcome == outcome
    if outcome == "failed":
        assert {item.status for item in run.evidence.placements} == {"failed"}
        assert {getattr(item, "detail", None) for item in run.evidence.placements} == {
            WORK_LIMIT_DETAIL
        }
        assert f"short of the {settle} s settle window" in run.diagnostic
    assert fake.calls()[-1]["argv"][7] == "delete"


def test_cd_a_placement_gap_is_not_evaluated_without_calling_kubectl(
    tmp_path: Path, fake: _FakeCluster
) -> None:
    stage = CdStage(tmp_path / "stage", str(uuid.uuid4()))
    stage.write(
        PLACED,
        [
            built("cd-waffle", {"amd64": AMD64}),
            built("cd-xycar", {"amd64": AMD64}),
        ],
        profile="waffle-xycar-amd64",
    )

    run = stage.run(serve("cd", partial(deploy, cluster=fake.cluster())))

    assert fake.calls() == []
    assert run.response.domain_outcome == "not_evaluated"
    assert run.evidence is not None
    assert run.evidence.manifest is None
    assert [
        (item.service_id, getattr(item, "reason", None))
        for item in run.evidence.placements
    ] == [("cd-waffle", "no-image-for-architecture"), ("cd-xycar", "blocked")]


def test_cd_hosts_without_a_ready_node_fail_execution_without_evidence(
    tmp_path: Path, fake: _FakeCluster
) -> None:
    fake.answer(nodes=_nodes(["xycar"]))
    stage = _stage(tmp_path)

    run = stage.run(
        serve("cd", partial(deploy, cluster=fake.cluster(node_wait_seconds=1)))
    )

    assert run.response.execution_conclusion == "failed"
    assert run.response.reason is not None
    assert run.response.reason.code == "sdi.cd.nodes-not-ready"
    assert run.evidence is None
    assert "no Ready node for every placed host" in run.diagnostic
    assert {call["argv"][7] for call in fake.calls()} == {"get"}


def test_cd_a_kubectl_failure_is_machinery_and_still_deletes_the_namespace(
    tmp_path: Path, fake: _FakeCluster
) -> None:
    fake.refuse("apply")
    stage = _stage(tmp_path)

    run = stage.run(serve("cd", partial(deploy, cluster=fake.cluster())))

    assert run.response.execution_conclusion == "failed"
    assert run.response.reason is not None
    assert run.response.reason.code == "sdi.cd.cluster-error"
    assert run.evidence is None
    assert "error: the fake cluster refused" in run.diagnostic
    assert [call["argv"][7] for call in fake.calls()][-2:] == ["apply", "delete"]


def test_cd_a_pod_still_starting_at_the_deadline_fails_on_the_work_limit(
    tmp_path: Path, fake: _FakeCluster
) -> None:
    fake.answer(
        pods=_pods({"cd-waffle": "ErrImagePull", "cd-xycar": "ContainerCreating"})
    )
    stage = _stage(tmp_path, work_limit_seconds=TEARDOWN_MARGIN_SECONDS + 3)

    run = stage.run(
        serve("cd", partial(deploy, cluster=fake.cluster(settle_seconds=0)))
    )

    assert run.response.execution_conclusion == "succeeded"
    assert run.evidence is not None
    assert run.evidence.domain_outcome == "failed"
    assert [
        (item.service_id, item.status, getattr(item, "detail", None))
        for item in run.evidence.placements
    ] == [
        ("cd-waffle", "failed", "ErrImagePull"),
        ("cd-xycar", "failed", WORK_LIMIT_DETAIL),
    ]
    assert "the work limit ran out in the wait for Running pods: cd-xycar" in (
        run.diagnostic
    )
    assert fake.calls()[-1]["argv"][7] == "delete"


@pytest.mark.parametrize(
    ("work_seconds", "round_trip"),
    [(work, trip) for trip in (0.5, 1.5, 4) for work in range(3, 11)],
)
def test_cd_a_poll_the_work_limit_cuts_off_fails_the_cd_with_evidence(
    tmp_path: Path, fake: _FakeCluster, work_seconds: int, round_trip: float
) -> None:
    fake.answer(pods=_pods({"cd-xycar": "ContainerCreating"}))
    fake.slow("pods", round_trip)
    stage = _stage(tmp_path, work_limit_seconds=TEARDOWN_MARGIN_SECONDS + work_seconds)

    run = stage.run(serve("cd", partial(deploy, cluster=fake.cluster())))

    assert run.response.reason is not None
    assert run.response.reason.code == "sdi.cd.failed", run.diagnostic
    assert run.evidence is not None
    assert [
        (item.service_id, getattr(item, "detail", None))
        for item in run.evidence.placements
    ] == [("cd-waffle", WORK_LIMIT_DETAIL), ("cd-xycar", WORK_LIMIT_DETAIL)]
    assert fake.calls()[-1]["argv"][7] == "delete"


def test_cd_a_poll_the_work_limit_cuts_off_keeps_the_last_observation(
    tmp_path: Path, fake: _FakeCluster
) -> None:
    fake.answer(
        pods=_pods({"cd-waffle": "ErrImagePull", "cd-xycar": "ContainerCreating"})
    )
    fake.slow("pods", 3, from_call=2)
    stage = _stage(tmp_path, work_limit_seconds=TEARDOWN_MARGIN_SECONDS + 4)

    run = stage.run(serve("cd", partial(deploy, cluster=fake.cluster())))

    assert run.response.reason is not None
    assert run.response.reason.code == "sdi.cd.failed", run.diagnostic
    assert run.evidence is not None
    assert [
        (item.service_id, getattr(item, "detail", None))
        for item in run.evidence.placements
    ] == [("cd-waffle", "ErrImagePull"), ("cd-xycar", WORK_LIMIT_DETAIL)]
    assert "kubectl get: its deadline passed before it answered" in run.diagnostic
    assert "the work limit ran out in the wait for Running pods: cd-xycar" in (
        run.diagnostic
    )
    assert [call["argv"][7] for call in fake.calls()] == [
        "get",
        "create",
        "apply",
        "get",
        "get",
        "get",
        "delete",
    ]


def test_cd_a_cluster_silent_past_the_call_cap_with_time_left_is_machinery(
    tmp_path: Path, fake: _FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_cd_adapter, "KUBECTL_TIMEOUT_SECONDS", 4)
    monkeypatch.setattr(_cd_adapter, "TEARDOWN_MARGIN_SECONDS", 12)
    fake.slow("pods", 13)
    stage = _stage(tmp_path, work_limit_seconds=12 + 30)

    started = time.monotonic()
    run = stage.run(serve("cd", partial(deploy, cluster=fake.cluster())))

    assert time.monotonic() - started < 2 * 4
    assert run.response.reason is not None
    assert run.response.reason.code == "sdi.cd.cluster-error"
    assert run.evidence is None
    assert "kubectl get did not answer within 4 s" in run.diagnostic
    assert [call["argv"][7] for call in fake.calls()] == [
        "get",
        "create",
        "apply",
        "get",
        "delete",
    ]


def test_cd_a_slow_cluster_cannot_carry_the_adapter_past_its_work_limit(
    tmp_path: Path, fake: _FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_cd_adapter, "KUBECTL_TIMEOUT_SECONDS", 4)
    monkeypatch.setattr(_cd_adapter, "TEARDOWN_MARGIN_SECONDS", 12)
    work_limit = 15
    fake.slow("pods", 13)
    fake.slow("delete", 13)
    stage = _stage(tmp_path, work_limit_seconds=work_limit)

    started = time.monotonic()
    run = stage.run(serve("cd", partial(deploy, cluster=fake.cluster())))

    assert time.monotonic() - started < work_limit
    assert run.response.reason is not None
    assert run.response.reason.code == "sdi.cd.cluster-error"
    assert run.evidence is None
    assert [call["argv"][7] for call in fake.calls()] == [
        "get",
        "create",
        "apply",
        "get",
        "get",
        "delete",
    ]
    assert "kubectl get: its deadline passed before it answered" in run.diagnostic
    assert "kubectl get did not answer within 4 s" in run.diagnostic
    assert "the namespace delete failed: kubectl delete did not answer within 4 s" in (
        run.diagnostic
    )
    assert "the work limit" not in run.diagnostic


def test_cd_a_cluster_silent_after_its_pods_ran_names_no_work_limit(
    tmp_path: Path, fake: _FakeCluster, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_cd_adapter, "KUBECTL_TIMEOUT_SECONDS", 4)
    monkeypatch.setattr(_cd_adapter, "TEARDOWN_MARGIN_SECONDS", 12)
    fake.slow("pods", 13, from_call=2)
    stage = _stage(tmp_path, work_limit_seconds=12 + 5)

    run = stage.run(serve("cd", partial(deploy, cluster=fake.cluster())))

    assert run.response.reason is not None
    assert run.response.reason.code == "sdi.cd.cluster-error"
    assert run.evidence is None
    assert "kubectl get did not answer within 4 s" in run.diagnostic
    assert "the work limit" not in run.diagnostic


def test_cd_the_minimum_work_limit_fits_the_longest_node_wait_and_two_calls() -> None:
    assert STAGES["cd"].grant.minimum_work_limit_seconds == (
        TEARDOWN_MARGIN_SECONDS
        + STAND_IN.node_wait_seconds
        + 2 * KUBECTL_TIMEOUT_SECONDS
    )
