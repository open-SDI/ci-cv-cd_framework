"""Checks for Stage profiles rendered from the slot registry."""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from sdi_pipeline_integration import _stage_contracts
from sdi_pipeline_integration._stage_contracts import (
    STAGE_GRANTS,
    Slot,
    StageProfile,
    render_stage_profile,
)

PROFILES = Path(__file__).parents[1] / "stage-profiles"
PROFILE_NAMES = [
    "cd-v1.yaml",
    "composition-v1.yaml",
    "cv-v1.yaml",
    "image-build-v1.yaml",
]


def _stage_profiles(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sdi-integration", "stage-profiles", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def test_write_renders_exactly_the_committed_profiles(tmp_path: Path) -> None:
    write = _stage_profiles("--write", "--directory", str(tmp_path))

    assert write.returncode == 0, write.stderr
    assert sorted(path.name for path in tmp_path.iterdir()) == PROFILE_NAMES
    for name in PROFILE_NAMES:
        assert (tmp_path / name).read_bytes() == (PROFILES / name).read_bytes()
    check = _stage_profiles("--check", "--directory", str(PROFILES))
    assert check.returncode == 0, check.stderr
    assert check.stdout == ""


def test_check_names_a_hand_edited_profile_and_write_restores_it(
    tmp_path: Path,
) -> None:
    shutil.copytree(PROFILES, tmp_path, dirs_exist_ok=True)
    profile = tmp_path / "cv-v1.yaml"
    profile.write_text(
        profile.read_text().replace(
            "work_limit_seconds: 2700", "work_limit_seconds: 1"
        ),
        encoding="utf-8",
    )

    stale = _stage_profiles("--check", "--directory", str(tmp_path))

    assert stale.returncode == 2
    assert stale.stderr == (
        "sdi-integration: generated Stage profiles are stale: ['cv-v1.yaml']\n"
    )
    assert _stage_profiles("--write", "--directory", str(tmp_path)).returncode == 0
    assert profile.read_bytes() == (PROFILES / "cv-v1.yaml").read_bytes()
    assert (tmp_path / "README.md").read_bytes() == (
        PROFILES / "README.md"
    ).read_bytes()


def test_check_reports_missing_and_obsolete_profiles(tmp_path: Path) -> None:
    shutil.copytree(PROFILES, tmp_path, dirs_exist_ok=True)
    (tmp_path / "cd-v1.yaml").unlink()
    (tmp_path / "retired-v1.yaml").write_text("stage: retired\n", encoding="utf-8")

    completed = _stage_profiles("--check", "--directory", str(tmp_path))

    assert completed.returncode == 2
    assert completed.stderr == (
        "sdi-integration: Stage profile set is stale; "
        "missing=['cd-v1.yaml'], obsolete=['retired-v1.yaml']\n"
    )


def test_a_slot_added_to_the_registry_reaches_the_rendered_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _stage_contracts,
        "SLOTS",
        (
            *_stage_contracts.SLOTS,
            Slot(
                "attestation",
                "attestation.json",
                "application/json",
                "sdi.attestation/v1",
                "image_build",
            ),
        ),
    )
    grant = STAGE_GRANTS["cd"]
    monkeypatch.setitem(
        STAGE_GRANTS,
        "cd",
        dataclasses.replace(grant, inputs=(*grant.inputs, "attestation")),
    )

    image_build = render_stage_profile("image_build")
    cd = render_stage_profile("cd")

    output = image_build.outputs[-1]
    assert (output.slot, output.path, output.role, output.max_bytes) == (
        "attestation",
        "outputs/attestation.json",
        "domain_output",
        STAGE_GRANTS["image_build"].domain_output_max_bytes,
    )
    assert (cd.inputs[-1].slot, cd.inputs[-1].path) == (
        "attestation",
        "attestation.json",
    )


@pytest.mark.parametrize(
    "output_path",
    ["/absolute/output.json", "../traversing/output.json"],
)
def test_profile_rejects_unconfined_output_grants(output_path: str) -> None:
    profile = render_stage_profile("composition").model_dump(mode="json")
    profile["outputs"][0]["path"] = output_path

    with pytest.raises(ValidationError, match="path"):
        StageProfile.model_validate(profile)


def test_profile_rejects_a_grant_on_a_reserved_path() -> None:
    profile = render_stage_profile("composition").model_dump(mode="json")
    profile["outputs"][0]["path"] = "response.json"

    with pytest.raises(ValidationError, match="path values must be unique"):
        StageProfile.model_validate(profile)
