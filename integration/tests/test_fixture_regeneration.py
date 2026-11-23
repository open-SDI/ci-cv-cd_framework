"""Black-box checks for the Fixture case regeneration command."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]
CASES = "integration/fixtures/cases"
# The cases and every input they are derived from.
REGENERATION_INPUTS = (
    "runs",
    "requirements",
    "profiles",
    CASES,
    "integration/tests/data",
)


def _regenerate(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "sdi_pipeline_integration._fixture_regeneration",
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _copy_inputs(root: Path) -> Path:
    for relative in REGENERATION_INPUTS:
        shutil.copytree(REPOSITORY_ROOT / relative, root / relative)
    return root


def test_committed_fixture_cases_are_fresh() -> None:
    completed = _regenerate("--check", "--repository", str(REPOSITORY_ROOT))

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""


def test_check_names_the_case_whose_digest_is_stale(tmp_path: Path) -> None:
    root = _copy_inputs(tmp_path)
    case = root / CASES / "cv-s-04-tc-03-c-05/case.yaml"
    committed = case.read_text(encoding="utf-8")
    case.write_text(
        committed.replace("byte_size: 8550", "byte_size: 8551", 1), encoding="utf-8"
    )

    completed = _regenerate("--check", "--repository", str(root))

    assert completed.returncode == 2
    assert completed.stderr == (
        "regenerate-fixtures: Fixture cases are stale: "
        f"['{CASES}/cv-s-04-tc-03-c-05/case.yaml']\n"
    )


def test_check_names_the_payload_whose_source_inputs_are_stale(
    tmp_path: Path,
) -> None:
    root = _copy_inputs(tmp_path)
    payload = root / CASES / "cd-s-04-tc-03-c-05/deployment-result.json"
    document = json.loads(payload.read_text(encoding="utf-8"))
    document["source_inputs"][0]["sha256"] = "0" * 64
    payload.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    completed = _regenerate("--check", "--repository", str(root))

    assert completed.returncode == 2
    assert completed.stderr == (
        "regenerate-fixtures: Fixture cases are stale: "
        f"['{CASES}/cd-s-04-tc-03-c-05/deployment-result.json']\n"
    )


def test_write_carries_a_config_change_through_every_stage(tmp_path: Path) -> None:
    root = _copy_inputs(tmp_path)
    config = root / "integration/tests/data/fixture-composition-config.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "timeout_seconds: 300", "timeout_seconds: 240"
        ),
        encoding="utf-8",
    )
    before = {path: path.read_bytes() for path in (root / CASES).rglob("*.*")}

    stale = _regenerate("--check", "--repository", str(root))
    written = _regenerate("--write", "--repository", str(root))
    fresh = _regenerate("--check", "--repository", str(root))

    assert stale.returncode == 2
    assert written.returncode == 0, written.stderr
    assert fresh.returncode == 0, fresh.stderr
    changed = sorted(
        str(path.relative_to(root / CASES))
        for path, content in before.items()
        if path.read_bytes() != content
    )
    # Every case matches the settings digest or a payload derived from it.
    assert changed == sorted(
        [
            *(f"{name}/case.yaml" for name in _case_names()),
            "composition-s-04-tc-03-c-05/composition-blueprint.json",
            "composition-s-04-tc-03-c-05/deployment-schema.json",
            "image-build-s-04-tc-03-c-05/image-build-result.json",
            "cv-s-04-tc-03-c-05/validation-evidence.json",
            "cd-s-04-tc-03-c-05/deployment-result.json",
        ]
    )


def test_write_leaves_fresh_cases_untouched(tmp_path: Path) -> None:
    root = _copy_inputs(tmp_path)
    before = {path: path.read_bytes() for path in (root / CASES).rglob("*.*")}

    written = _regenerate("--write", "--repository", str(root))

    assert written.returncode == 0, written.stderr
    assert {path: path.read_bytes() for path in before} == before


def _case_names() -> list[str]:
    return [path.name for path in (REPOSITORY_ROOT / CASES).iterdir() if path.is_dir()]
