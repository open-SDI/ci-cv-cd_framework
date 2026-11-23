"""Compare every hand-kept copy of the Stage wiring with the Stage registry."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from ruamel.yaml import YAML

from sdi_pipeline_integration._handoff_contracts import HANDOFF_PARAMETERS
from sdi_pipeline_integration._stage_registry import (
    AGENTS,
    INTEGRATION_AGENT,
    RUN_LIMITS,
    STAGE_ORDER,
    STAGES,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

SOURCE_ROOT = Path(__file__).resolve().parents[2]
JENKINS_ROOT = SOURCE_ROOT / "deployment" / "jenkins"
CASC = JENKINS_ROOT / "casc" / "jenkins.yaml"
COMPOSE = JENKINS_ROOT / "compose.yaml"
SMOKE_CHECK = JENKINS_ROOT / "bin" / "smoke-check"
STACK = JENKINS_ROOT / "bin" / "stack"
JOB_DSL = JENKINS_ROOT / "jobs" / "pipeline-integration.groovy"
JENKINSFILE = SOURCE_ROOT / "Jenkinsfile"
WORKFLOW = SOURCE_ROOT / ".github" / "workflows" / "pipeline-integration.yml"
REGISTRY = "_stage_registry.py"
JOB_NAME_ENTRY = re.compile(r"^\s*'([a-z][a-z0-9-]*)': [A-Za-z_.]+,$")

type Entries = dict[Any, tuple[int, Any]]


def drift(
    path: Path, entries: Entries, expected: Mapping[Any, object], owner: str
) -> list[str]:
    where = path.relative_to(SOURCE_ROOT) if path.is_relative_to(SOURCE_ROOT) else path
    failures = [
        f"{where}:{line}: {key!r} is not in {owner}"
        for key, (line, _value) in entries.items()
        if key not in expected
    ]
    failures += [
        f"{where}:{line}: {key!r} is {value!r}; {owner} has {expected[key]!r}"
        for key, (line, value) in entries.items()
        if key in expected and value != expected[key]
    ]
    failures += [
        f"{where}: {key!r} from {owner} is missing"
        + ("" if value is None else f"; it has {value!r}")
        for key, value in expected.items()
        if key not in entries
    ]
    return failures


def _yaml(path: Path) -> Any:  # noqa: ANN401
    return YAML().load(path)  # pyright: ignore[reportUnknownMemberType]


def casc_nodes(path: Path) -> Entries:
    nodes = _yaml(path)["jenkins"]["nodes"]
    return {
        node["permanent"]["name"]: (
            node["permanent"].lc.line + 1,
            node["permanent"]["labelString"],
        )
        for node in nodes
    }


def compose_agents(path: Path) -> Entries:
    services = _yaml(path)["services"]
    return {
        name: (
            services.lc.key(name)[0] + 1,
            (
                service["environment"]["JENKINS_AGENT_NAME"],
                service["build"]["args"]["SDI_JENKINS_AGENT_LABEL"],
            ),
        )
        for name, service in services.items()
        if "JENKINS_AGENT_NAME" in service.get("environment", {})
    }


def _python_constants(path: Path) -> Entries:
    module = ast.parse(path.read_text(encoding="utf-8"))
    constants: Entries = {}
    for statement in module.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            if isinstance(target, ast.Name):
                try:
                    value = ast.literal_eval(statement.value)
                except ValueError:
                    continue
                constants[target.id] = (statement.lineno, value)
    return constants


def smoke_check_agents(path: Path) -> Entries:
    line, agents = _python_constants(path)["EXPECTED_AGENTS"]
    return {node: (line, label) for node, label in agents.items()}


def smoke_check_parameters(path: Path) -> Entries:
    line, parameters = _python_constants(path)["EXPECTED_PARAMETERS"]
    return dict.fromkeys(parameters, (line, None))


def smoke_check_jobs(path: Path) -> Entries:
    constants = _python_constants(path)
    return {
        constants[name][1]: (constants[name][0], None)
        for name in ("UPSTREAM_JOB", "FORK_JOB")
    }


def _matching_lines(path: Path, pattern: re.Pattern[str]) -> Entries:
    return {
        match.group(1): (number, match.group(2) if pattern.groups > 1 else None)
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if (match := pattern.search(line))
    }


def job_dsl_parameters(path: Path) -> Entries:
    return _matching_lines(path, re.compile(r"stringParam\('([A-Z_]+)'"))


def job_names(path: Path) -> Entries:
    return _matching_lines(path, JOB_NAME_ENTRY)


def workflow_timeout(path: Path) -> Entries:
    return _matching_lines(path, re.compile(r"^\s*(timeout-minutes): ([0-9]+)$"))


def _text_matches(
    path: Path, pattern: re.Pattern[str]
) -> list[tuple[int, re.Match[str]]]:
    text = path.read_text(encoding="utf-8")
    return [
        (text.count("\n", 0, match.start()) + 1, match)
        for match in pattern.finditer(text)
    ]


def casc_credentials(path: Path) -> Entries:
    return {
        credential["string"]["id"]: (credential["string"].lc.key("id")[0] + 1, None)
        for domain in _yaml(path)["credentials"]["system"]["domainCredentials"]
        for credential in domain["credentials"]
    }


def stack_adapter_stages(path: Path) -> Entries:
    ((_line, command),) = _text_matches(path, STACK_ADAPTER_IMAGE)
    text = path.read_text(encoding="utf-8")
    return {
        index: (text.count("\n", 0, stage.start()) + 1, stage.group(1))
        for index, stage in enumerate(
            STACK_STAGE.finditer(text, command.start(), command.end())
        )
    }


def stack_image_variables(path: Path) -> Entries:
    return {
        index: (line, match.group(1))
        for index, (line, match) in enumerate(_text_matches(path, STACK_IMAGE_READ))
    }


STACK_ADAPTER_IMAGE = re.compile(r"sdi-integration adapter-image [^)]*\)")
STACK_STAGE = re.compile(r"--stage ([a-z_]+)")
STACK_IMAGE_READ = re.compile(
    r"^\s*read -r (JENKINS_[A-Z_]+_AGENT_IMAGE)$", re.MULTILINE
)
AGENT_LABELS = {agent.node: agent.label for agent in AGENTS}
ORDER = "_stage_registry.STAGE_ORDER"

type Parser = Callable[[Path], Entries]

STAGE_COPIES: list[tuple[Path, Parser, Mapping[Any, object], str]] = [
    (
        CASC,
        casc_credentials,
        dict.fromkeys(
            credential.jenkins_id
            for stage in STAGES.values()
            for credential in stage.credentials
        ),
        REGISTRY,
    ),
    (STACK, stack_adapter_stages, dict(enumerate(STAGE_ORDER)), ORDER),
    (
        STACK,
        stack_image_variables,
        {
            index: f"JENKINS_{name.upper()}_AGENT_IMAGE"
            for index, name in enumerate(STAGE_ORDER)
        },
        ORDER,
    ),
]


def test_jcasc_nodes_match_the_registry_agents() -> None:
    failures = drift(CASC, casc_nodes(CASC), AGENT_LABELS, REGISTRY)
    assert not failures, "\n".join(failures)


def test_compose_agent_services_match_the_registry_agents() -> None:
    expected = {
        agent.node: (agent.node, "" if agent == INTEGRATION_AGENT else agent.label)
        for agent in AGENTS
    }
    failures = drift(COMPOSE, compose_agents(COMPOSE), expected, REGISTRY)
    assert not failures, "\n".join(failures)


def test_smoke_check_agents_match_the_registry_agents() -> None:
    failures = drift(
        SMOKE_CHECK, smoke_check_agents(SMOKE_CHECK), AGENT_LABELS, REGISTRY
    )
    assert not failures, "\n".join(failures)


def test_job_parameters_match_the_handoff_contract() -> None:
    expected = dict.fromkeys(HANDOFF_PARAMETERS)
    owner = "_handoff_contracts.HANDOFF_PARAMETERS"
    failures = drift(JOB_DSL, job_dsl_parameters(JOB_DSL), expected, owner)
    failures += drift(SMOKE_CHECK, smoke_check_parameters(SMOKE_CHECK), expected, owner)
    assert not failures, "\n".join(failures)


def test_job_names_match_the_job_dsl() -> None:
    expected = dict.fromkeys(job_names(JOB_DSL))
    assert expected
    owner = str(JOB_DSL.relative_to(SOURCE_ROOT))
    failures = drift(JENKINSFILE, job_names(JENKINSFILE), expected, owner)
    failures += drift(SMOKE_CHECK, smoke_check_jobs(SMOKE_CHECK), expected, owner)
    assert not failures, "\n".join(failures)


def test_workflow_timeout_matches_the_run_limits() -> None:
    expected = {"timeout-minutes": str(RUN_LIMITS.github_job_seconds // 60)}
    failures = drift(WORKFLOW, workflow_timeout(WORKFLOW), expected, REGISTRY)
    assert not failures, "\n".join(failures)


def test_drift_names_the_file_and_line_of_each_copy(tmp_path: Path) -> None:
    casc = tmp_path / "jenkins.yaml"
    casc.write_text(
        CASC.read_text(encoding="utf-8").replace(
            'labelString: "cv"', 'labelString: "validation"'
        ),
        encoding="utf-8",
    )
    lines = casc.read_text(encoding="utf-8").splitlines()
    cv_line, cd_line = (
        lines.index(f'        name: "{node}"') + 1 for node in ("cv", "cd")
    )
    expected = AGENT_LABELS | {"gazebo": "gazebo"}
    del expected["cd"]

    assert drift(casc, casc_nodes(casc), expected, REGISTRY) == [
        f"{casc}:{cd_line}: 'cd' is not in {REGISTRY}",
        f"{casc}:{cv_line}: 'cv' is 'validation'; {REGISTRY} has 'cv'",
        f"{casc}: 'gazebo' from {REGISTRY} is missing; it has 'gazebo'",
    ]


@pytest.mark.parametrize(
    ("path", "parser", "expected", "owner"),
    STAGE_COPIES,
    ids=[f"{path.name}-{parser.__name__}" for path, parser, _e, _o in STAGE_COPIES],
)
def test_stage_wiring_copies_match_the_registry(
    path: Path, parser: Parser, expected: Mapping[Any, object], owner: str
) -> None:
    failures = drift(path, parser(path), expected, owner)
    assert not failures, "\n".join(failures)


INJECTED_DRIFT: dict[tuple[Path, Parser], tuple[str, str]] = {
    (CASC, casc_credentials): ('id: "sdi-openai-api-key"', 'id: "sdi-openai-key"'),
    (STACK, stack_adapter_stages): ("--stage cv --stage cd", "--stage cd --stage cv"),
    (STACK, stack_image_variables): ("read -r JENKINS_CV_", "read -r JENKINS_GAZEBO_"),
}


@pytest.mark.parametrize(
    ("path", "parser", "expected", "owner"),
    STAGE_COPIES,
    ids=[f"{path.name}-{parser.__name__}" for path, parser, _e, _o in STAGE_COPIES],
)
def test_injected_drift_names_its_file_and_line(
    tmp_path: Path,
    path: Path,
    parser: Parser,
    expected: Mapping[Any, object],
    owner: str,
) -> None:
    committed, drifted = INJECTED_DRIFT[path, parser]
    copy = tmp_path / path.name
    text = path.read_text(encoding="utf-8")
    assert committed in text
    copy.write_text(text.replace(committed, drifted, 1), encoding="utf-8")
    line = text.count("\n", 0, text.index(committed)) + 1

    failures = drift(copy, parser(copy), expected, owner)

    assert any(failure.startswith(f"{copy}:{line}: ") for failure in failures), failures
