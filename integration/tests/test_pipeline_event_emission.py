"""The pipeline events the CLI and the Stage runtime print, checked as printed."""

from __future__ import annotations

import json
import re
import signal
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pytest

from sdi_pipeline_integration import _stage_runtime
from sdi_pipeline_integration._git_input import GitRepository
from sdi_pipeline_integration._local_dispatch import dispatch_local
from sdi_pipeline_integration._pipeline_event_check import (
    ConsoleEvent,
    Projection,
    read_console,
)
from sdi_pipeline_integration._pipeline_event_printer import (
    RUN_ACCEPTED,
    RUN_FINISHED,
    EventPrinter,
)
from sdi_pipeline_integration._run_input import identify_committed_run
from sdi_pipeline_integration._stage_profiles import stage_profile_bytes
from sdi_pipeline_integration._stage_registry import STAGES
from sdi_pipeline_integration._stage_runtime import (
    AdapterTimeLimit,
    SuppliedInputs,
    adapter_time_limit,
    committed_stage_sources,
    execute_identified_stage,
    stage_inputs,
)
from tests import _cd_evidence as cd_evidence
from tests import _cv_evidence as cv_evidence
from tests import _pipeline_event_world as world
from tests import test_composition_stage as composition_stage
from tests._event_console import Console, assert_conforms, assert_no_events
from tests._fixture_inputs import (
    FIXTURE_COMPOSITION_CONFIG,
    FIXTURE_SERVICE_REPOSITORY,
    RED_RUN_EXIT,
    SUPPLIED_INPUT_ARGUMENTS,
    commit_fixture_repository,
    write_fixture_composition_descriptor,
)
from tests._pipeline_events import golden, golden_names
from tests._scripted_stages import (
    CD_IMAGES,
    CD_REASONS,
    CV_PLACEMENTS,
    CV_REASONS,
    EXECUTION_ID,
    EXIT_NONZERO,
    HANG,
    LEAVE_DESCENDANT,
    SIGNAL_SELF,
    SUCCESS_REQUEST,
    JenkinsRun,
    git,
    implemented_upstream,
    jenkins_repository,
    millis_from_now,
    mirror_local_overrides,
    respond,
    run_preflight,
    script_adapter,
    script_stage,
    select_evidence_adapter,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from sdi_pipeline_integration._pipeline_event_check import Golden
    from sdi_pipeline_integration._stage_registry import StageName


def _projections(stderr: bytes) -> list[str]:
    items, violations = read_console(stderr)
    assert violations == []
    return [
        str(Projection.of(item.row)) for item in items if isinstance(item, ConsoleEvent)
    ]


@pytest.mark.parametrize(
    ("work_limit", "deadline", "expected"),
    [
        (600, None, AdapterTimeLimit(600.0, "work-limit-reached")),
        (600, 1000.0 + 600, AdapterTimeLimit(600.0, "work-limit-reached")),
        (600, 1000.0 + 599.5, AdapterTimeLimit(599.5, "run-deadline-reached")),
        (600, 1000.0 - 5, AdapterTimeLimit(0.0, "run-deadline-reached")),
    ],
    ids=["no-deadline", "deadline-equal-to-limit", "deadline-sooner", "expired"],
)
def test_the_adapter_waits_for_the_work_limit_unless_the_deadline_is_sooner(
    work_limit: int, deadline: float | None, expected: AdapterTimeLimit
) -> None:
    assert adapter_time_limit(work_limit, deadline, 1000.0) == expected


@pytest.mark.parametrize(
    ("seconds", "limit_min"),
    [(0.0, 1), (59.9, 1), (60.0, 1), (119.9, 1), (120.0, 2), (2700.0, 45)],
)
def test_the_shown_limit_rounds_down_to_at_least_one_minute(
    seconds: float, limit_min: int
) -> None:
    limit = AdapterTimeLimit(seconds, "work-limit-reached")

    assert limit.limit_min == limit_min
    assert limit.running_line("cv").slots == {"limit_min": limit_min}
    assert limit.reached_line("cv").slots == {"limit_min": limit_min}


def test_each_time_limit_names_its_own_reason_and_line() -> None:
    work = AdapterTimeLimit(60.0, "work-limit-reached")
    run = AdapterTimeLimit(0.0, "run-deadline-reached")

    assert work.reason[0] == "sdi.stage.deadline-exceeded"
    assert run.reason[0] == "sdi.run.deadline-exceeded"
    assert work.reached_line("cv").step == "work-limit-reached"
    assert run.reached_line("cv").step == "run-deadline-reached"
    assert run.reached_line("cv").slots == {}


def test_a_printer_prints_one_line_pair_per_emit_on_stderr(
    capfd: pytest.CaptureFixture[str],
) -> None:
    printer = EventPrinter.for_run(
        world.IDENTIFIED,
        {"target_profile": world.profile_source()},
        scope="run",
    )

    printer.emit(RUN_ACCEPTED)
    printer.with_executions([world.composition_ok()]).emit(RUN_FINISHED)

    captured = capfd.readouterr()
    assert captured.out == ""
    assert _projections(captured.err.encode()) == [
        "RUN run-accepted [P P P P] -",
        "RUN run-finished final [S P P P] -",
    ]


type World = Callable[[Path], Path]
type Route = Literal["local", "jenkins"]

FAILED_MISSION_STEP = cv_evidence.evidence(
    "failed",
    [cv_evidence.participated(item) for item in cv_evidence.SERVICE_IDS],
    cv_evidence.mission("navigate-to-rendezvous", "failed"),
)
CRASH_LOOPING = cd_evidence.result(
    "failed",
    [
        cd_evidence.deployed("fixture-localization"),
        cd_evidence.crash_looping("fixture-navigation")
        | {"k8s_reason": "CrashLoopBackOff"},
        cd_evidence.deployed("fixture-mission-control"),
    ],
)
IMAGE_NEVER_PULLED = cd_evidence.result(
    "failed",
    [
        cd_evidence.deployed("fixture-localization"),
        cd_evidence.deployed("fixture-navigation"),
        {
            "service_id": "fixture-mission-control",
            "host": cd_evidence.HOSTS["fixture-mission-control"],
            "status": "failed",
            "detail": "ImagePullBackOff: the image was never built",
            "k8s_reason": "ImagePullBackOff",
        },
    ],
)
ONE_IMAGE_FAILED = CD_IMAGES | {
    "fixture-mission-control": {
        "service_id": "fixture-mission-control",
        "status": "failed",
        "reason": "build-failed",
        "detail": "the image build exited unsuccessfully",
    }
}


def _every_stage(
    tmp_path: Path,
    *,
    images: dict[str, dict[str, Any]] = CD_IMAGES,
    cv: dict[str, Any] = cv_evidence.SUCCEEDED,
    cd: dict[str, Any] = cd_evidence.SUCCEEDED,
) -> Path:
    repository = implemented_upstream(tmp_path, images, placements=CV_PLACEMENTS)
    outcome = cv["domain_outcome"]
    select_evidence_adapter(repository, "cv", outcome, cv, CV_REASONS[outcome])
    outcome = cd["domain_outcome"]
    select_evidence_adapter(repository, "cd", outcome, cd, CD_REASONS[outcome])
    return repository


def _scripted(stage: StageName, body: str) -> World:
    def world(tmp_path: Path) -> Path:
        repository = _every_stage(tmp_path)
        script_adapter(repository, stage, body)
        return repository

    return world


def _missing_cv_adapter(tmp_path: Path) -> Path:
    repository = _every_stage(tmp_path)
    script_stage(repository, "cv", tmp_path / "missing-adapter")
    return repository


SHARED_WORLDS: dict[str, World] = {
    "g1": _every_stage,
    "g2": lambda tmp_path: _every_stage(tmp_path, cv=FAILED_MISSION_STEP),
    "g2b": lambda tmp_path: _every_stage(tmp_path, cv=cv_evidence.FAILED),
    "g3": lambda tmp_path: _every_stage(tmp_path, cd=CRASH_LOOPING),
    "g4": _scripted(
        "image_build",
        respond(
            "failed",
            "not_evaluated",
            {
                "category": "dependency",
                "code": "sdi.image-build.builder-unreachable",
                "summary": "The image builder did not answer.",
            },
        ),
    ),
    "g5": _scripted(
        "composition",
        respond(
            "succeeded",
            "failed",
            {
                "category": "domain",
                "code": "sdi.composition.scoped-rejection",
                "summary": "Every proposal lost all of its services.",
            },
        ),
    ),
    "g16": _missing_cv_adapter,
    "g17": _scripted("cv", EXIT_NONZERO),
    "g17-signaled": _scripted("cv", SIGNAL_SELF),
    "g17-descendants": _scripted("cv", LEAVE_DESCENDANT),
    "g18": _scripted(
        "image_build",
        respond("succeeded", "succeeded", documents={"image-build-result.json": "{}"}),
    ),
    "g21": lambda tmp_path: _every_stage(
        tmp_path,
        images=ONE_IMAGE_FAILED,
        cv=cv_evidence.NOT_EVALUATED,
        cd=IMAGE_NEVER_PULLED,
    ),
}
G17_REASONS = {
    "g17-signaled": "sdi.adapter.process-signaled",
    "g17-descendants": "sdi.adapter.descendants-survived",
}


def _golden(case: str) -> Golden:
    if case not in G17_REASONS:
        return golden(case)
    base = golden("g17")
    code = base.lines[-1].projection.failure_reason
    return replace(
        base,
        lines=tuple(
            replace(
                line,
                projection=replace(line.projection, failure_reason=G17_REASONS[case]),
            )
            if line.projection.failure_reason == code
            else line
            for line in base.lines
        ),
    )


def _commit(repository: Path) -> str:
    mirror_local_overrides(repository)
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Select scripted Stage adapters")
    return git(repository, "rev-parse", "HEAD")


def _dispatch_arguments(repository: Path, commit_sha: str, root: Path) -> list[str]:
    return [
        "dispatch-local",
        "--repository",
        str(repository),
        "--requested-ref",
        "refs/heads/main",
        "--resolved-commit",
        commit_sha,
        "--run-request-path",
        SUCCESS_REQUEST,
        "--bundle-root",
        str(root / "bundle"),
        *SUPPLIED_INPUT_ARGUMENTS,
    ]


def _local_route(repository: Path, commit_sha: str, root: Path) -> Console:
    console = Console()
    dispatched = console.run(*_dispatch_arguments(repository, commit_sha, root))
    assert dispatched.returncode in {0, RED_RUN_EXIT}, dispatched.stderr
    return console


def _jenkins_route(repository: Path, commit_sha: str, root: Path) -> Console:
    run = JenkinsRun(repository, commit_sha, root)
    conclusion, _result = run.chain()
    assert conclusion.returncode in {0, RED_RUN_EXIT}, conclusion.stderr
    return run.console


ROUTES: dict[Route, Callable[[Path, str, Path], Console]] = {
    "local": _local_route,
    "jenkins": _jenkins_route,
}


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("case", SHARED_WORLDS)
def test_either_route_prints_the_golden_lines(
    tmp_path: Path, case: str, route: Route
) -> None:
    repository = SHARED_WORLDS[case](tmp_path)

    console = ROUTES[route](repository, _commit(repository), tmp_path / "run")

    assert_conforms(console, _golden(case))


@pytest.mark.parametrize("route", ROUTES)
def test_an_all_fixture_run_prints_g15(tmp_path: Path, route: Route) -> None:
    if route == "local":
        repository, commit_sha = commit_fixture_repository(tmp_path)
    else:
        repository, commit_sha = jenkins_repository(tmp_path, fixture_image_build=True)

    console = ROUTES[route](repository, commit_sha, tmp_path / "run")

    assert_conforms(console, golden("g15"))


def _jenkins_run(tmp_path: Path, world: World = _every_stage) -> JenkinsRun:
    repository = world(tmp_path)
    run = JenkinsRun(repository, _commit(repository), tmp_path / "run")
    preflighted = run.preflight()
    assert preflighted.returncode == 0, preflighted.stderr
    return run


def _run_stages(run: JenkinsRun, *stages: StageName) -> None:
    for stage in stages:
        executed = run.execute(stage)
        assert executed.returncode == 0, executed.stderr
        assert run.continues(stage)


def _in_process(
    capfd: pytest.CaptureFixture[str],
    repository: Path,
    *,
    run_deadline_monotonic: float | None = None,
) -> Console:
    """Dispatch in this process; `write_event` writes to fd 2, which capfd sees."""
    dispatch_local(
        repository_path=repository,
        requested_ref="refs/heads/main",
        resolved_commit=_commit(repository),
        run_request_path=SUCCESS_REQUEST,
        bundle_root=repository.parent / "bundle",
        supplied=SuppliedInputs(
            service_repository=FIXTURE_SERVICE_REPOSITORY,
            composition_config=FIXTURE_COMPOSITION_CONFIG,
        ),
        run_deadline_monotonic=run_deadline_monotonic,
    )
    printed = capfd.readouterr()
    assert printed.out == ""
    console = Console()
    console.add("", printed.err)
    return console


@pytest.mark.slow
def test_a_jenkins_stage_past_its_work_limit_prints_g6(tmp_path: Path) -> None:
    repository = _scripted("cv", HANG)(tmp_path)
    run = JenkinsRun(repository, _commit(repository), tmp_path / "run")

    run.chain(work_limits={"cv": "1"})

    assert_conforms(run.console, golden("g6"))


@pytest.mark.slow
def test_a_local_stage_past_its_work_limit_prints_g6(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    repository = _scripted("cv", HANG)(tmp_path)
    cv = STAGES["cv"]
    monkeypatch.setitem(
        STAGES,
        "cv",
        replace(cv, grant=replace(cv.grant, work_limit_seconds=1)),
    )
    (repository / "integration/stage-profiles/cv-v1.yaml").write_bytes(
        stage_profile_bytes("cv")
    )

    console = _in_process(capfd, repository)

    assert_conforms(console, golden("g6"))


@pytest.mark.slow
def test_a_run_deadline_inside_a_jenkins_stage_prints_g7(tmp_path: Path) -> None:
    run = _jenkins_run(tmp_path, _scripted("cv", HANG))
    _run_stages(run, "composition", "image_build")

    executed = run.execute("cv", deadline_millis=millis_from_now(5))
    if executed.returncode == 3:
        pytest.skip("the run deadline passed before CV's adapter started")

    assert executed.returncode == 0, executed.stderr
    assert not run.continues("cv")
    assert run.finalize().returncode == 0
    assert_conforms(run.console, golden("g7"))


@pytest.mark.parametrize("case", ["g8", "g9-no-node"])
def test_a_run_deadline_before_cd_prints_only_the_final_line_for_it(
    tmp_path: Path, case: str
) -> None:
    run = _jenkins_run(tmp_path)
    _run_stages(run, "composition", "image_build", "cv")
    if case == "g8":
        expired = run.execute("cd", deadline_millis=millis_from_now(-1))
        assert expired.returncode == 3
        assert_no_events(expired.stderr)

    finalized = run.finalize(run_deadline_expired=True)

    assert finalized.returncode == 0, finalized.stderr
    assert_conforms(run.console, golden(case))


def test_a_killed_cv_leaves_its_lines_and_finalize_prints_g9(tmp_path: Path) -> None:
    run = _jenkins_run(tmp_path, _scripted("cv", HANG))
    _run_stages(run, "composition", "image_build")

    killed = run.console.interrupt(
        *run.stage_arguments("cv", deadline_millis=millis_from_now(60)),
        after="CV > adapter-running",
    )
    finalized = run.finalize(run_deadline_expired=True)

    assert killed.returncode == 128 + signal.SIGTERM, killed.stderr
    assert finalized.returncode == 0, finalized.stderr
    assert_conforms(run.console, golden("g9-killed-cv"))


def test_a_cv_killed_before_its_stash_prints_the_g9_race(tmp_path: Path) -> None:
    run = _jenkins_run(tmp_path)
    _run_stages(run, "composition", "image_build", "cv")

    finalized = run.finalize(attempts=run.attempts[:2], run_deadline_expired=True)

    assert finalized.returncode == 0, finalized.stderr
    assert_conforms(run.console, golden("g9-race"))


def test_a_finalize_with_no_attempts_prints_only_the_final_line(
    tmp_path: Path,
) -> None:
    repository, commit_sha = jenkins_repository(tmp_path)
    run = JenkinsRun(repository, commit_sha, tmp_path / "run")

    finalized = run.finalize(run_deadline_expired=True)
    conclusion = run.conclusion()

    assert finalized.returncode == 0, finalized.stderr
    assert conclusion.returncode == RED_RUN_EXIT, conclusion.stderr
    assert_conforms(run.console, golden("g10"))


def test_a_stale_cv_agent_prints_nothing_more(tmp_path: Path) -> None:
    run = _jenkins_run(tmp_path)
    _run_stages(run, "composition", "image_build")

    stale = run.execute("cv", baked="missing")

    assert stale.returncode == 4, stale.stderr
    assert_no_events(stale.stderr)
    assert_conforms(run.console, golden("g11"))


def test_an_aborted_local_run_prints_nothing_more(tmp_path: Path) -> None:
    repository = _scripted("cv", HANG)(tmp_path)
    console = Console()

    aborted = console.interrupt(
        *_dispatch_arguments(repository, _commit(repository), tmp_path / "run"),
        after="CV > adapter-running",
    )

    assert aborted.returncode == 128 + signal.SIGTERM, aborted.stderr
    assert_conforms(console, golden("g12"))


def test_a_stage_that_raises_after_stage_started_prints_nothing_more(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    repository = _every_stage(tmp_path)
    image_build = str(tmp_path / "implemented-image-build-adapter")
    popen = subprocess.Popen

    def fail_image_build(
        arguments: Sequence[str],
        *rest: Any,  # noqa: ANN401
        **options: Any,  # noqa: ANN401
    ) -> subprocess.Popen[Any]:
        if arguments[0] == image_build:
            msg = "the runtime broke after stage-started"
            raise RuntimeError(msg)
        return popen(arguments, *rest, **options)

    monkeypatch.setattr(_stage_runtime.subprocess, "Popen", fail_image_build)

    with pytest.raises(RuntimeError, match="after stage-started"):
        _in_process(capfd, repository)

    printed = Console([capfd.readouterr().err.encode()])
    assert_conforms(printed, golden("g13"))


def test_a_failure_after_the_final_line_prints_nothing_more(tmp_path: Path) -> None:
    repository = _every_stage(tmp_path)
    run = JenkinsRun(repository, _commit(repository), tmp_path / "run")
    conclusion, _result = run.chain()
    assert conclusion.returncode == 0, conclusion.stderr
    (run.bundle_root / "pipeline-integration-result.json").write_text("{}")

    broken = run.conclusion()

    assert broken.returncode == 2
    assert_no_events(broken.stderr)
    assert_conforms(run.console, golden("g14"))


def test_dispatch_local_past_its_run_deadline_prints_g19(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    repository = _scripted("composition", HANG)(tmp_path)

    console = _in_process(capfd, repository, run_deadline_monotonic=time.monotonic())

    assert_conforms(console, golden("g19"))


def test_a_single_image_build_stage_prints_g20(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    repository_path = _every_stage(tmp_path)
    commit_sha = _commit(repository_path)
    repository = GitRepository(repository_path, commit_sha)
    identified = identify_committed_run(
        repository_path=repository_path,
        requested_ref="refs/heads/main",
        resolved_commit=commit_sha,
        run_request_path=SUCCESS_REQUEST,
    )
    supplied = SuppliedInputs(
        service_repository=FIXTURE_SERVICE_REPOSITORY,
        composition_config=FIXTURE_COMPOSITION_CONFIG,
    )
    events = EventPrinter.for_run(
        identified,
        committed_stage_sources(repository, identified),
        scope="single_stage",
    )
    composition = execute_identified_stage(
        repository=repository,
        identified=identified,
        descriptor_path=STAGES["composition"].jenkins_descriptor,
        attempt_root=tmp_path / "attempts" / "composition",
        available_inputs=stage_inputs(
            repository, identified, supplied, stage="composition"
        ),
        events=events,
    )
    capfd.readouterr()

    execute_identified_stage(
        repository=repository,
        identified=identified,
        descriptor_path=STAGES["image_build"].jenkins_descriptor,
        attempt_root=tmp_path / "attempts" / "image_build",
        available_inputs=stage_inputs(
            repository, identified, supplied, stage="image_build"
        )
        | composition.outputs,
        events=events,
    )

    assert_conforms(Console([capfd.readouterr().err.encode()]), golden("g20"))


def test_execute_stage_prints_only_its_stage_lines(tmp_path: Path) -> None:
    repository, commit_sha = commit_fixture_repository(tmp_path)
    console = Console()

    executed = console.run(*_execute_stage_arguments(repository, commit_sha, tmp_path))

    assert executed.returncode == 0, executed.stderr
    assert json.loads(executed.stdout)["correlation"]["stage"] == "composition"
    assert_conforms(console, None)
    assert _projections(console.stderr) == [
        f"composition {step} [{status} P P P] {reason}"
        for step, status, reason in (
            ("stage-started", "R", "-"),
            ("adapter-running", "R", "-"),
            ("checking-output", "R", "-"),
            ("publishing", "R", "-"),
            ("stage-finished", "N", "sdi.fixture.composition-executed"),
        )
    ]


def test_a_pre_spawn_skip_prints_only_stage_finished(tmp_path: Path) -> None:
    repository, _commit_sha = commit_fixture_repository(tmp_path)
    write_fixture_composition_descriptor(repository, mode="not_implemented")
    git(repository, "commit", "-am", "Leave composition unimplemented")
    console = Console()

    executed = console.run(
        *_execute_stage_arguments(
            repository, git(repository, "rev-parse", "HEAD"), tmp_path
        )
    )

    assert executed.returncode == 0, executed.stderr
    assert_conforms(console, None)
    assert [line.split()[1] for line in _projections(console.stderr)] == [
        "stage-finished"
    ]


def _execute_stage_arguments(
    repository: Path, commit_sha: str, tmp_path: Path
) -> list[str]:
    return [
        "execute-stage",
        "--repository",
        str(repository),
        "--requested-ref",
        "refs/heads/main",
        "--resolved-commit",
        commit_sha,
        "--run-request-path",
        SUCCESS_REQUEST,
        "--descriptor-path",
        STAGES["composition"].jenkins_descriptor,
        "--attempt-root",
        str(tmp_path / "attempt"),
        *SUPPLIED_INPUT_ARGUMENTS,
    ]


def test_commands_that_are_not_printers_print_nothing(tmp_path: Path) -> None:
    repository, commit_sha = jenkins_repository(tmp_path, fixture_image_build=True)
    stale = run_preflight(
        repository,
        commit_sha,
        SUCCESS_REQUEST,
        millis_from_now(-1),
        baked={"cv": "missing"},
    )
    run = JenkinsRun(repository, commit_sha, tmp_path / "run")
    run.chain()
    composition = str(run.root / "transfers" / "composition")
    silent = [
        (
            "attempt-allows-continuation",
            "--attempt-root",
            composition,
            "--execution-id",
            EXECUTION_ID,
            "--stage",
            "composition",
        ),
        (
            "bundle-conclusion",
            "--bundle-root",
            str(run.bundle_root),
            "--execution-id",
            EXECUTION_ID,
        ),
        (
            "deployment-manifest",
            "--bundle-root",
            str(run.bundle_root),
            "--output",
            str(tmp_path / "manifest.yaml"),
        ),
        ("integration-code-digest", "--root", str(repository / "integration")),
    ]

    completed = [
        subprocess.run(
            ["sdi-integration", *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        for arguments in silent
    ]

    assert stale.returncode == 4
    assert_no_events(stale.stderr)
    for arguments, process in zip(silent, completed, strict=True):
        assert process.returncode in {0, RED_RUN_EXIT}, (arguments, process.stderr)
        assert_no_events(process.stderr)


def test_only_the_printer_builds_and_writes_event_rows() -> None:
    source = Path(_stage_runtime.__file__).parent
    callers = {
        path.name
        for path in source.rglob("*.py")
        if re.search(r"\b(build_row|write_event)\(", path.read_text())
    }

    assert callers <= {
        "_pipeline_event_rows.py",
        "_pipeline_events.py",
        "_pipeline_event_printer.py",
    }


DRIVES: dict[str, Callable[..., None]] = {
    **dict.fromkeys(
        (_golden(case).name for case in SHARED_WORLDS),
        test_either_route_prints_the_golden_lines,
    ),
    "g6": test_a_jenkins_stage_past_its_work_limit_prints_g6,
    "g7": test_a_run_deadline_inside_a_jenkins_stage_prints_g7,
    "g8": test_a_run_deadline_before_cd_prints_only_the_final_line_for_it,
    "g9-no-node": test_a_run_deadline_before_cd_prints_only_the_final_line_for_it,
    "g9-killed-cv": test_a_killed_cv_leaves_its_lines_and_finalize_prints_g9,
    "g9-race": test_a_cv_killed_before_its_stash_prints_the_g9_race,
    "g10": test_a_finalize_with_no_attempts_prints_only_the_final_line,
    "g11": test_a_stale_cv_agent_prints_nothing_more,
    "g12": test_an_aborted_local_run_prints_nothing_more,
    "g13": test_a_stage_that_raises_after_stage_started_prints_nothing_more,
    "g14": test_a_failure_after_the_final_line_prints_nothing_more,
    "g15": test_an_all_fixture_run_prints_g15,
    "g15b": composition_stage.test_publishes_the_preferred_stubbed_proposal,
    "g19": test_dispatch_local_past_its_run_deadline_prints_g19,
    "g20": test_a_single_image_build_stage_prints_g20,
}
EXEMPT: dict[str, str] = {}


def test_every_golden_is_driven_by_a_real_run_or_exempt() -> None:
    assert not DRIVES.keys() & EXEMPT.keys()
    assert sorted(DRIVES.keys() | EXEMPT.keys()) == golden_names()
