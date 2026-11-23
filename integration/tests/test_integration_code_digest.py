"""Checks for the digest of the integration code an agent image bakes."""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from tests._fixture_inputs import copy_baked_integration_code

if TYPE_CHECKING:
    from pathlib import Path


def _digest(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sdi-integration", "integration-code-digest", "--root", str(root)],
        check=False,
        capture_output=True,
        text=True,
    )


def _baked_tree(tmp_path: Path, name: str) -> Path:
    repository = tmp_path / name
    copy_baked_integration_code(repository)
    return repository / "integration"


def _digest_of(root: Path) -> str:
    completed = _digest(root)
    assert completed.returncode == 0, completed.stderr
    assert re.fullmatch(r"[0-9a-f]{64}\n", completed.stdout)
    return completed.stdout.strip()


def test_identical_baked_trees_share_one_digest(tmp_path: Path) -> None:
    assert _digest_of(_baked_tree(tmp_path, "one")) == _digest_of(
        _baked_tree(tmp_path, "two")
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        "pyproject.toml",
        "uv.lock",
        "src/sdi_pipeline_integration/_cli.py",
        "fixtures/cases/added-case.json",
    ],
)
def test_a_baked_change_changes_the_digest(tmp_path: Path, relative_path: str) -> None:
    root = _baked_tree(tmp_path, "repository")
    before = _digest_of(root)
    changed = root / relative_path
    changed.parent.mkdir(parents=True, exist_ok=True)
    with changed.open("ab") as stream:
        _ = stream.write(b"\n")

    assert _digest_of(root) != before


@pytest.mark.parametrize(
    "relative_path",
    [
        "README.md",
        "tests/test_cli.py",
        "schemas/added.schema.json",
        "stage-profiles/cv-v1.yaml",
        "src/sdi_pipeline_integration/__pycache__/_cli.cpython-312.pyc",
        "src/sdi_pipeline_integration/stray.pyc",
    ],
)
def test_changes_outside_the_baked_code_keep_the_digest(
    tmp_path: Path, relative_path: str
) -> None:
    root = _baked_tree(tmp_path, "repository")
    before = _digest_of(root)
    changed = root / relative_path
    changed.parent.mkdir(parents=True, exist_ok=True)
    _ = changed.write_bytes(b"not baked\n")

    assert _digest_of(root) == before


def test_a_moved_file_changes_the_digest(tmp_path: Path) -> None:
    root = _baked_tree(tmp_path, "repository")
    before = _digest_of(root)
    source = root / "src/sdi_pipeline_integration/_cli.py"
    _ = source.rename(root / "src/sdi_pipeline_integration/_moved_cli.py")

    assert _digest_of(root) != before


@pytest.mark.parametrize("missing", ["pyproject.toml", "uv.lock", "src", "fixtures"])
def test_a_missing_baked_root_is_rejected(tmp_path: Path, missing: str) -> None:
    root = _baked_tree(tmp_path, "repository")
    path = root / missing
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()

    completed = _digest(root)

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert missing in completed.stderr


def test_a_symlink_in_the_baked_code_is_rejected(tmp_path: Path) -> None:
    root = _baked_tree(tmp_path, "repository")
    (root / "src/linked.py").symlink_to(root / "src/sdi_pipeline_integration/_cli.py")

    completed = _digest(root)

    assert completed.returncode == 2
    assert "src/linked.py" in completed.stderr
