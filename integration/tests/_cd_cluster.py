from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import requests

K3S_VARIABLES = ("SDI_K3S_SERVER_URL", "SDI_K3S_KUBECONFIG", "SDI_K3S_REGISTRY_URL")


@dataclass(frozen=True)
class SessionCluster:
    server: str
    kubeconfig: Path
    registry_url: str

    def kubectl(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="sdi-k3s-test-") as cache:
            return subprocess.run(
                [
                    shutil.which("kubectl") or "kubectl",
                    "--kubeconfig",
                    str(self.kubeconfig),
                    "--server",
                    self.server,
                    "--cache-dir",
                    cache,
                    "--request-timeout=10s",
                    *arguments,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )


def session_cluster() -> SessionCluster:
    server, kubeconfig, registry_url = (os.environ[name] for name in K3S_VARIABLES)
    return SessionCluster(server, Path(kubeconfig), registry_url)


def cluster_problem() -> str | None:
    missing = [name for name in K3S_VARIABLES if not os.environ.get(name)]
    if missing:
        return f"{', '.join(missing)} must name the session cluster"
    if shutil.which("kubectl") is None:
        return "kubectl is not on PATH"
    cluster = session_cluster()
    try:
        answered = cluster.kubectl("get", "--raw", "/readyz").returncode == 0
    except subprocess.TimeoutExpired:
        answered = False
    if not answered:
        return f"the API server at {cluster.server} does not answer"
    try:
        requests.get(f"{cluster.registry_url}/v2/", timeout=10).raise_for_status()
    except requests.RequestException as error:
        return (
            f"the registry at {cluster.registry_url} is unreachable: "
            f"{type(error).__name__}"
        )
    return None
