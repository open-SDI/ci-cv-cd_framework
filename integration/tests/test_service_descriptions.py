"""Public-boundary checks for reading an SDI service repository."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[2]
SERVICE_REPOSITORY = REPOSITORY_ROOT / "service-repository"
FAKE_DESCRIPTIONS = Path(__file__).parent / "data/service-descriptions"

VALID = """\
---
schema_version: sdi.service-description/v1
service_id: {service_id}
title: Test-only service
provenance: {{basis: placeholder, note: Test-only hypothetical description.}}
depends_on: {depends_on}
---

## Purpose

Test-only hypothetical service.
"""


def _validate(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "sdi-integration",
            "validate-service-repository",
            "--service-repository",
            str(directory),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _write(directory: Path, relative: str, content: str) -> None:
    path = directory / relative
    path.parent.mkdir(parents=True)
    path.write_text(content)


def _valid(service_id: str, depends_on: str = "[]") -> str:
    return VALID.format(service_id=service_id, depends_on=depends_on)


def test_lists_every_real_and_fake_description_with_its_digest(
    tmp_path: Path,
) -> None:
    combined = tmp_path / "services"
    shutil.copytree(SERVICE_REPOSITORY, combined)
    shutil.copytree(FAKE_DESCRIPTIONS, combined / "fakes")

    completed = _validate(combined)

    assert completed.returncode == 0, completed.stderr
    expected = sorted(
        (
            {
                "path": path.relative_to(combined).as_posix(),
                "service_id": path.parent.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in combined.rglob("SDI.md")
        ),
        key=lambda item: item["path"],
    )
    assert any(item["path"].startswith("fakes/") for item in expected)
    assert json.loads(completed.stdout) == {"services": expected}


@pytest.mark.parametrize(
    ("files", "message"),
    [
        (
            {"a/SDI.md": "## Purpose\n\nNo front matter.\n"},
            "a/SDI.md: expected YAML front matter",
        ),
        (
            {"a/SDI.md": _valid("a").split("## Purpose")[0]},
            "a/SDI.md: Markdown body is empty",
        ),
        (
            {"a/SDI.md": _valid("a").replace("title:", "owner: me\ntitle:")},
            "a/SDI.md: contract validation failed",
        ),
        (
            {
                "a/SDI.md": _valid("a").replace(
                    "depends_on:",
                    "artifacts:\n  debs:\n    architectures: [arm64]\n"
                    "    host_requirement: {os: ubuntu-24.04, ros_distro: jazzy}\n"
                    "    route: {kind: apt, packages: [ros-jazzy-nav2-bringup]}\n"
                    "depends_on:",
                )
            },
            "a/SDI.md: contract validation failed",
        ),
        (
            {"a/SDI.md": _valid("same"), "b/SDI.md": _valid("same")},
            "b/SDI.md: duplicate service_id same (also a/SDI.md)",
        ),
        (
            {"a/SDI.md": _valid("a", "[missing]")},
            "a/SDI.md: depends_on names unknown service missing",
        ),
        (
            {"a/SDI.md": _valid("a", "[b]"), "b/SDI.md": _valid("b", "[a]")},
            "depends_on cycle",
        ),
        ({}, "no SDI.md found"),
    ],
)
def test_rejects_an_invalid_service_repository(
    tmp_path: Path, files: dict[str, str], message: str
) -> None:
    tmp_path.joinpath("services").mkdir()
    for relative, content in files.items():
        _write(tmp_path / "services", relative, content)

    completed = _validate(tmp_path / "services")

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert message in completed.stderr
