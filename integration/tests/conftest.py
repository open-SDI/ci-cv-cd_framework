"""Fail a session that selects cluster-backed tests without their cluster."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest
import requests

from ._cd_cluster import cluster_problem

if TYPE_CHECKING:
    from collections.abc import Callable


def _engine_problem() -> str | None:
    import docker  # noqa: PLC0415
    from docker.errors import DockerException  # noqa: PLC0415

    engine = os.environ.get("SDI_CV_ENGINE_URL")
    registry = os.environ.get("SDI_CV_REGISTRY_URL")
    if not engine or not registry:
        return "SDI_CV_ENGINE_URL and SDI_CV_REGISTRY_URL must name the engine pair"
    try:
        docker.DockerClient(base_url=engine, timeout=10).api.ping()
    except (DockerException, requests.RequestException) as error:
        return f"the engine at {engine} is unreachable: {type(error).__name__}"
    try:
        requests.get(f"{registry}/v2/", timeout=10).raise_for_status()
    except requests.RequestException as error:
        return f"the registry at {registry} is unreachable: {type(error).__name__}"
    return None


PROBLEMS: dict[str, Callable[[], str | None]] = {
    "cv_engine": _engine_problem,
    "k3s": cluster_problem,
}


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for marker, find_problem in PROBLEMS.items():
        if not any(item.get_closest_marker(marker) for item in items):
            continue
        problem = find_problem()
        if problem is not None:
            message = f"{marker} tests are selected, but {problem}"
            sys.stderr.write(f"{message}\n")
            pytest.exit(message, returncode=pytest.ExitCode.USAGE_ERROR)
