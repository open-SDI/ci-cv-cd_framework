"""Fail a session that selects cv_engine tests without a reachable CV engine pair."""

from __future__ import annotations

import os
import sys

import pytest
import requests


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


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if not any(item.get_closest_marker("cv_engine") for item in items):
        return
    problem = _engine_problem()
    if problem is not None:
        message = f"cv_engine tests are selected, but {problem}"
        sys.stderr.write(f"{message}\n")
        pytest.exit(message, returncode=pytest.ExitCode.USAGE_ERROR)
