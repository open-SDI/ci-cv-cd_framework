"""Black-box checks for the single repository acceptance entry point."""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

REPOSITORY_ROOT = Path(__file__).parents[2]
VERIFY = REPOSITORY_ROOT / "integration" / "scripts" / "verify"
ADAPTERS = REPOSITORY_ROOT / "deployment" / "jenkins" / "adapters"


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


def _fake_tools(tmp_path: Path) -> Path:
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
        'if [ -n "${STAGE_STATUS_PYTHON-}" ]; then\n'
        '  case "$*" in "run --frozen sdi-integration stage-status "*)\n'
        "    shift 3\n"
        '    exec "$STAGE_STATUS_PYTHON" -m sdi_pipeline_integration "$@"\n'
        "  esac\n"
        "fi\n"
        '[ "$*" != "${FAIL_ACCEPTANCE_COMMAND-}" ]\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    return fake_bin


def _environment(tmp_path: Path) -> dict[str, str]:
    return os.environ | {
        "ACCEPTANCE_LOG": str(tmp_path / "acceptance.log"),
        "PATH": f"{_fake_tools(tmp_path)}:{os.environ['PATH']}",
        "SDI_HOST_LOCK": str(tmp_path / "host.lock"),
    }


def _survivors(tmp_path: Path) -> list[str]:
    marker = f"ACCEPTANCE_LOG={tmp_path / 'acceptance.log'}".encode()
    survivors: list[str] = []
    for process in Path("/proc").iterdir():
        try:
            if marker in (process / "environ").read_bytes().split(b"\0"):
                survivors.append(process.name)
        except OSError:
            continue
    return survivors


def _eventually(condition: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + 5
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.05)
    return condition()


@pytest.fixture(autouse=True)
def no_process_outlives_its_test(tmp_path: Path) -> Generator[None]:
    yield
    assert _eventually(lambda: not _survivors(tmp_path)), "processes outlived the test"


def _kill_group(process: subprocess.Popen[str]) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


@contextlib.contextmanager
def _process_group(
    arguments: list[str | Path],
    checkout: Path,
    environment: dict[str, str],
    *,
    timeout: float,
) -> Generator[subprocess.Popen[str]]:
    process = subprocess.Popen(
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=checkout,
        env=environment,
        text=True,
        start_new_session=True,
    )
    watchdog = threading.Timer(timeout, _kill_group, [process])
    watchdog.start()
    try:
        yield process
    finally:
        watchdog.cancel()
        _kill_group(process)
        process.communicate()


def _run(
    arguments: list[str | Path],
    checkout: Path,
    environment: dict[str, str],
    *,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    with _process_group(arguments, checkout, environment, timeout=timeout) as process:
        stdout, stderr = process.communicate()
    return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)


def _git(checkout: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
        check=True,
        capture_output=True,
        cwd=checkout,
    )


def test_verification_is_the_complete_fail_fast_acceptance_gate(tmp_path: Path) -> None:
    checkout, verify = _acceptance_checkout(tmp_path)
    shutil.copytree(ADAPTERS, checkout / "deployment" / "jenkins" / "adapters")
    log = tmp_path / "acceptance.log"
    environment = _environment(tmp_path) | {"STAGE_STATUS_PYTHON": sys.executable}

    completed = _run([verify], checkout, environment)

    assert completed.returncode == 0, completed.stderr
    passed_commands = log.read_text(encoding="utf-8").splitlines()
    assert passed_commands == [
        "uv lock --check",
        "uv sync --frozen --all-groups",
        "uv run --frozen sdi-integration schemas --check --directory schemas",
        (
            "uv run --frozen sdi-integration stage-profiles --check "
            "--directory stage-profiles"
        ),
        (
            "uv run --frozen sdi-integration jenkinsfile --check "
            "--jenkinsfile ../Jenkinsfile"
        ),
        (
            "uv run --frozen python -m "
            "sdi_pipeline_integration._fixture_regeneration --check"
        ),
        (
            "uv run --frozen check-jsonschema --quiet --builtin-schema "
            "vendor.github-workflows ../.github/workflows/pipeline-integration.yml"
        ),
        "uv run --frozen ruff format --check .",
        "uv run --frozen ruff check .",
        "uv run --frozen basedpyright",
        "uv run --frozen pytest -m not slow -n auto --dist worksteal",
        "uv run --frozen pytest -m slow",
        "uv tree --frozen --all-groups",
        "stack smoke",
        "uv run --frozen sdi-integration stage-status --repository ..",
    ]
    assert "uv 0.12.1" in completed.stdout
    assert "SDI_GITHUB_RUNNER_VERSION=2.337.0" in completed.stdout
    assert "workflow-cps:4378.v7a_08f1b_b_f8f4" in completed.stdout
    stage_status = subprocess.run(
        [
            sys.executable,
            "-m",
            "sdi_pipeline_integration",
            "stage-status",
            "--repository",
            REPOSITORY_ROOT,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert stage_status == (
        "composition: implemented (jenkins), implemented (local)\n"
        "image-build: implemented (jenkins), fixture (local)\n"
        "cv: fixture (jenkins), fixture (local)\n"
        "cd: fixture (jenkins), fixture (local)\n"
    )
    assert completed.stdout.endswith(f"Repository acceptance passed.\n{stage_status}")

    failed = _run(
        [verify],
        checkout,
        environment | {"FAIL_ACCEPTANCE_COMMAND": "run --frozen basedpyright"},
    )

    assert failed.returncode != 0
    assert not any(
        line.startswith("uv run --frozen pytest")
        for line in log.read_text(encoding="utf-8").splitlines()[len(passed_commands) :]
    )
    assert "Repository acceptance passed." not in failed.stdout


def test_verification_fails_when_stage_status_cannot_read_a_descriptor(
    tmp_path: Path,
) -> None:
    checkout, verify = _acceptance_checkout(tmp_path)
    environment = _environment(tmp_path) | {"STAGE_STATUS_PYTHON": sys.executable}

    completed = _run([verify], checkout, environment)

    assert completed.returncode != 0
    assert "sdi-integration: " in completed.stderr
    assert "Repository acceptance passed." not in completed.stdout


def _commands(tmp_path: Path) -> list[str]:
    return (tmp_path / "acceptance.log").read_text(encoding="utf-8").splitlines()


def _verify(
    verify: Path,
    checkout: Path,
    environment: dict[str, str],
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return _run([verify, *arguments], checkout, environment)


def _branch_checkout(tmp_path: Path) -> tuple[Path, Path]:
    checkout, verify = _acceptance_checkout(tmp_path)
    tracked = checkout / "integration" / "src" / "tracked.py"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("tracked = 1\n", encoding="utf-8")
    _git(checkout, "init", "--quiet", "--initial-branch", "main")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "--quiet", "--message", "base")
    _git(checkout, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(checkout, "switch", "--quiet", "--create", "change")
    return checkout, verify


def _write(checkout: Path, path: str) -> None:
    changed = checkout / path
    changed.parent.mkdir(parents=True, exist_ok=True)
    with changed.open("a", encoding="utf-8") as file:
        file.write("change = 1\n")


def _commit(checkout: Path, path: str) -> None:
    _write(checkout, path)
    _git(checkout, "add", path)
    _git(checkout, "commit", "--quiet", "--message", f"change {path}")


def _smoke_decision(completed: subprocess.CompletedProcess[str], tmp_path: Path) -> str:
    assert completed.returncode == 0, completed.stderr
    [decision] = [
        line
        for line in completed.stdout.splitlines()
        if line.startswith("stack smoke ")
    ]
    assert ("stack smoke" in _commands(tmp_path)) is decision.startswith(
        "stack smoke runs: "
    )
    return decision


SMOKE_INPUTS_CHANGED = (
    "stack smoke runs: a smoke input changed since the merge base with origin/main"
)


def test_fast_tier_skips_the_slow_tests_and_the_stack_smoke(tmp_path: Path) -> None:
    checkout, verify = _acceptance_checkout(tmp_path)

    completed = _verify(verify, checkout, _environment(tmp_path), "--fast")

    assert completed.returncode == 0, completed.stderr
    commands = _commands(tmp_path)
    assert commands[-1] == "uv run --frozen pytest -m not slow -n auto --dist worksteal"
    assert "uv run --frozen pytest -m slow" not in commands
    assert "stack smoke" not in commands
    assert completed.stdout.endswith(
        "fast tier passed; run integration/scripts/verify before opening a PR\n"
    )


def test_stack_smoke_runs_when_head_is_the_merge_base(tmp_path: Path) -> None:
    checkout, verify = _branch_checkout(tmp_path)
    _write(checkout, "integration/tests/test_changed.py")

    completed = _verify(verify, checkout, _environment(tmp_path))

    assert _smoke_decision(completed, tmp_path) == (
        "stack smoke runs: HEAD is the merge base with origin/main"
    )


@pytest.mark.parametrize(
    "changed_path",
    [
        "deployment/jenkins/controller/changed.txt",
        "deployment/installation.env",
        "Jenkinsfile",
        "integration/src/sdi_pipeline_integration/changed.py",
        "integration/src/café.py",
        "integration/fixtures/changed.json",
        "integration/pyproject.toml",
        "integration/uv.lock",
        "integration/README.md",
        "integration/.python-version",
    ],
)
def test_stack_smoke_runs_when_a_branch_changes_a_smoke_input(
    tmp_path: Path, *, changed_path: str
) -> None:
    checkout, verify = _branch_checkout(tmp_path)
    _commit(checkout, changed_path)

    completed = _verify(verify, checkout, _environment(tmp_path))

    assert _smoke_decision(completed, tmp_path) == SMOKE_INPUTS_CHANGED


def test_stack_smoke_is_skipped_when_a_branch_changes_only_tests(
    tmp_path: Path,
) -> None:
    checkout, verify = _branch_checkout(tmp_path)
    _commit(checkout, "integration/tests/test_changed.py")

    completed = _verify(verify, checkout, _environment(tmp_path))

    assert _smoke_decision(completed, tmp_path) == (
        "stack smoke skipped: no smoke inputs changed "
        "since the merge base with origin/main"
    )


@pytest.mark.parametrize("staged", [False, True], ids=["untracked", "staged"])
@pytest.mark.parametrize("name", ["new.py", "café.py"])
def test_stack_smoke_runs_when_an_uncommitted_smoke_input_changed(
    tmp_path: Path, *, name: str, staged: bool
) -> None:
    checkout, verify = _branch_checkout(tmp_path)
    _commit(checkout, "integration/tests/test_changed.py")
    _write(checkout, f"integration/src/{name}")
    if staged:
        _git(checkout, "add", f"integration/src/{name}")

    completed = _verify(verify, checkout, _environment(tmp_path))

    assert _smoke_decision(completed, tmp_path) == SMOKE_INPUTS_CHANGED


def test_stack_smoke_runs_when_a_file_moves_out_of_a_smoke_input(
    tmp_path: Path,
) -> None:
    checkout, verify = _branch_checkout(tmp_path)
    (checkout / "integration" / "tests").mkdir()
    _git(checkout, "mv", "integration/src/tracked.py", "integration/tests/tracked.py")
    _git(checkout, "commit", "--quiet", "--message", "move")

    completed = _verify(verify, checkout, _environment(tmp_path))

    assert _smoke_decision(completed, tmp_path) == SMOKE_INPUTS_CHANGED


def _host_lock_spelling(tmp_path: Path, spelling: str) -> str:
    host_lock = tmp_path / "host.lock"
    host_lock.touch()
    match spelling:
        case "symlink":
            link = tmp_path / "link.lock"
            link.symlink_to(host_lock)
            return str(link)
        case "double slash":
            return f"{tmp_path}//host.lock"
        case "relative":
            return "../host.lock"
        case _:
            return str(host_lock)


HOST_LOCK_SPELLINGS = ["absolute", "symlink", "double slash", "relative"]


@pytest.mark.parametrize("spelling", HOST_LOCK_SPELLINGS)
def test_a_caller_holding_the_host_lock_does_not_deadlock_verification(
    tmp_path: Path, *, spelling: str
) -> None:
    checkout, verify = _acceptance_checkout(tmp_path)
    host_lock = _host_lock_spelling(tmp_path, spelling)
    environment = _environment(tmp_path) | {"SDI_HOST_LOCK": host_lock}

    completed = _run(["flock", host_lock, verify], checkout, environment, timeout=10)

    assert completed.returncode == 0, completed.stderr
    assert "waiting for" not in completed.stdout
    assert "stack smoke" in _commands(tmp_path)


@pytest.mark.parametrize("spelling", HOST_LOCK_SPELLINGS)
def test_verification_waits_while_another_process_holds_the_host_lock(
    tmp_path: Path, *, spelling: str
) -> None:
    checkout, verify = _acceptance_checkout(tmp_path)
    environment = _environment(tmp_path) | {
        "SDI_HOST_LOCK": _host_lock_spelling(tmp_path, spelling)
    }

    with (tmp_path / "host.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with _process_group([verify], checkout, environment, timeout=10) as verifying:
            assert verifying.stdout is not None
            waiting = next(
                (
                    line
                    for line in verifying.stdout
                    if line.startswith("verify: waiting")
                ),
                None,
            )
            assert waiting == f"verify: waiting for {tmp_path / 'host.lock'}\n"
            assert "uv run --frozen pytest -m slow" not in _commands(tmp_path)
            fcntl.flock(held, fcntl.LOCK_UN)
            _, stderr = verifying.communicate()
    assert verifying.returncode == 0, stderr
    commands = _commands(tmp_path)
    assert "uv run --frozen pytest -m slow" in commands
    assert "stack smoke" in commands


def test_stopping_a_waiting_verification_leaves_no_process_behind(
    tmp_path: Path,
) -> None:
    checkout, verify = _acceptance_checkout(tmp_path)

    with (tmp_path / "host.lock").open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with _process_group(
            [verify], checkout, _environment(tmp_path), timeout=10
        ) as verifying:
            assert verifying.stdout is not None
            assert any(line.startswith("verify: waiting") for line in verifying.stdout)
            assert _eventually(lambda: len(_survivors(tmp_path)) >= 2), (
                "verify started no lock waiter"
            )

        assert verifying.returncode == -signal.SIGKILL
        assert _eventually(lambda: not _survivors(tmp_path)), (
            "the lock waiter outlived verify"
        )


def test_full_verification_fails_clearly_without_flock(tmp_path: Path) -> None:
    checkout, verify = _acceptance_checkout(tmp_path)
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in ("dirname", "git", "grep", "readlink", "realpath"):
        found = shutil.which(tool)
        assert found is not None
        (tools / tool).symlink_to(found)
    environment = _environment(tmp_path)
    environment["PATH"] = f"{tmp_path / 'bin'}:{tools}"

    completed = _verify(verify, checkout, environment)

    assert completed.returncode != 0
    assert completed.stderr == "verify: flock(1) is required for the host lock\n"
    assert "waiting for" not in completed.stdout
