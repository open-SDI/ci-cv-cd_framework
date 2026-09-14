"""Black-box checks for the single repository acceptance entry point."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]
VERIFY = REPOSITORY_ROOT / "integration" / "scripts" / "verify"


def _acceptance_checkout(tmp_path: Path) -> tuple[Path, Path]:
    checkout = tmp_path / "checkout"
    scripts = checkout / "integration" / "scripts"
    plugins = checkout / "deployment" / "jenkins" / "controller"
    stack = checkout / "deployment" / "jenkins" / "bin" / "stack"
    scripts.mkdir(parents=True)
    plugins.mkdir(parents=True)
    stack.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(VERIFY, scripts / "verify")
    (checkout / "deployment" / "installation.env").write_text(
        "SDI_GITHUB_RUNNER_VERSION=2.337.0\n",
        encoding="utf-8",
    )
    (plugins / "plugins.txt").write_text(
        "workflow-cps:4378.v7a_08f1b_b_f8f4\n",
        encoding="utf-8",
    )
    stack.write_text(
        '#!/bin/sh\nprintf \'stack %s\\n\' "$*" >> "$ACCEPTANCE_LOG"\n',
        encoding="utf-8",
    )
    stack.chmod(0o755)
    return checkout, scripts / "verify"


def _fake_uv(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'if [ "${1-}" = --version ]; then\n'
        "  printf '%s\\n' 'uv 0.12.1 (x86_64-unknown-linux-gnu)'\n"
        "  exit 0\n"
        "fi\n"
        'printf \'uv %s\\n\' "$*" >> "$ACCEPTANCE_LOG"\n'
        '[ "$*" != "${FAIL_ACCEPTANCE_COMMAND-}" ]\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    return fake_bin


def test_verification_is_the_complete_fail_fast_acceptance_gate(tmp_path: Path) -> None:
    checkout, verify = _acceptance_checkout(tmp_path)
    fake_bin = _fake_uv(tmp_path)
    log = tmp_path / "acceptance.log"
    environment = os.environ | {
        "ACCEPTANCE_LOG": str(log),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }

    completed = subprocess.run(
        [verify],
        check=False,
        capture_output=True,
        cwd=checkout,
        env=environment,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "uv lock --check",
        "uv sync --frozen --all-groups",
        "uv run --frozen sdi-integration schemas --check --directory schemas",
        (
            "uv run --frozen check-jsonschema --quiet --builtin-schema "
            "vendor.github-workflows ../.github/workflows/pipeline-integration.yml"
        ),
        "uv run --frozen ruff format --check .",
        "uv run --frozen ruff check .",
        "uv run --frozen basedpyright",
        "uv run --frozen pytest",
        "uv tree --frozen --all-groups",
        "stack smoke",
    ]
    assert "uv 0.12.1" in completed.stdout
    assert "SDI_GITHUB_RUNNER_VERSION=2.337.0" in completed.stdout
    assert "workflow-cps:4378.v7a_08f1b_b_f8f4" in completed.stdout
    assert "Repository acceptance passed." in completed.stdout
    assert "permanent integration scaffold and Fixture behavior" in completed.stdout
    assert "Real Domain stages, KPI evaluation, product security assurance" in (
        completed.stdout
    )

    failed = subprocess.run(
        [verify],
        check=False,
        capture_output=True,
        cwd=checkout,
        env=environment | {"FAIL_ACCEPTANCE_COMMAND": "run --frozen basedpyright"},
        text=True,
    )

    assert failed.returncode != 0
    assert (
        "uv run --frozen pytest"
        not in log.read_text(encoding="utf-8").splitlines()[10:]
    )
    assert "Repository acceptance passed." not in failed.stdout
