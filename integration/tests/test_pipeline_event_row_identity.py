"""A pipeline-event row's identity: the run's ids and the profile's target."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from sdi_pipeline_integration._pipeline_event_rows import EventLine
from tests._pipeline_event_world import IDENTIFIED, PROFILES, build, profile_source

if TYPE_CHECKING:
    from pathlib import Path

ACCEPTED = EventLine(None, "run-accepted")


def _profile(kind: str, hosts: dict[str, tuple[str, str]]) -> bytes:
    document = {
        "schema_version": "sdi.target-execution-profile/v1",
        "profile_id": "test-profile",
        "provenance": {"basis": "placeholder", "note": "A test profile."},
        "target": {"target_id": "test-robot", "kind": kind},
        "hosts": {
            name: {"role": role, "architecture": architecture}
            for name, (role, architecture) in hosts.items()
        },
    }
    return json.dumps(document).encode()


@pytest.mark.parametrize(
    "path", sorted(PROFILES.glob("*.yaml")), ids=lambda path: path.stem
)
def test_every_committed_profile_resolves_to_one_architecture(path: Path) -> None:
    row = build(line=ACCEPTED, profile=profile_source(path.stem))

    assert row.architecture is not None, f"{path.name} has an ambiguous architecture"
    assert row.target is not None


def test_identity_comes_from_the_identified_run() -> None:
    row = build(line=ACCEPTED)

    assert (
        row.execution_id,
        row.composition_id,
        row.scenario_id,
        row.testcase_id,
        row.profile_id,
    ) == (
        IDENTIFIED["execution_id"],
        IDENTIFIED["combination_id"],
        IDENTIFIED["scenario_id"],
        IDENTIFIED["testcase_id"],
        IDENTIFIED["profile_id"],
    )
    assert (row.target, row.architecture) == ("SDV", "arm64")


@pytest.mark.parametrize(
    ("kind", "hosts", "expected"),
    [
        ("sdv", {"robot": ("native", "arm64")}, ("SDV", "arm64")),
        (
            "sdr",
            {"robot": ("native", "arm64"), "edge": ("edge", "arm64")},
            ("SDR", "arm64"),
        ),
        (
            "sdr",
            {"robot": ("native", "arm64"), "pc": ("controller", "amd64")},
            ("SDR", "amd64"),
        ),
        (
            "sdv",
            {
                "robot": ("native", "arm64"),
                "pc": ("controller", "amd64"),
                "edge": ("edge", "amd64"),
            },
            ("SDV", "amd64"),
        ),
        (
            "sdv",
            {"edge": ("edge", "arm64"), "pc": ("controller", "amd64")},
            (None, None),
        ),
        (
            "sdv",
            {"robot": ("native", "arm64"), "other": ("native", "amd64")},
            (None, None),
        ),
    ],
    ids=[
        "one-host",
        "all-share",
        "differs-from-native",
        "two-hosts-differ-alike",
        "no-native-host",
        "two-native-hosts",
    ],
)
def test_architecture_is_shared_or_differs_from_the_native_host(
    kind: str,
    hosts: dict[str, tuple[str, str]],
    expected: tuple[str | None, str | None],
) -> None:
    profile = profile_source(content=_profile(kind, hosts))

    row = build(line=ACCEPTED, profile=profile)

    assert (row.target, row.architecture) == expected


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\xff\xfe not utf-8",
        b"hosts: [unclosed",
        b"schema_version: sdi.target-execution-profile/v1\n",
        b"- a list\n- not a mapping\n",
    ],
    ids=["empty", "not-utf-8", "broken-yaml", "missing-fields", "not-a-mapping"],
)
def test_an_unreadable_profile_nulls_target_and_architecture(content: bytes) -> None:
    row = build(line=ACCEPTED, profile=profile_source(content=content))

    assert (row.target, row.architecture) == (None, None)


def test_a_profile_that_is_not_yaml_nulls_target_and_architecture() -> None:
    profile = replace(profile_source(), media_type="application/json")

    row = build(line=ACCEPTED, profile=profile)

    assert (row.target, row.architecture) == (None, None)
