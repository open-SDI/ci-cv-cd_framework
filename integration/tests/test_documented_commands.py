"""Acceptance inventory and safe execution for documented commands."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Literal

from markdown_it import MarkdownIt
from pydantic import BaseModel, ConfigDict
from ruamel.yaml import YAML

REPOSITORY_ROOT = Path(__file__).parents[2]
INTEGRATION_ROOT = REPOSITORY_ROOT / "integration"
INVENTORY = INTEGRATION_ROOT / "acceptance" / "documented-commands.yaml"
GUIDES = (
    "docs/pipeline-integration-trigger.md",
    "deployment/jenkins/README.md",
    "deployment/github-runner/README.md",
    "docs/scaffold-maintainer.md",
    "docs/installation-readiness-checklist.md",
)
type AcceptanceMode = Literal[
    "automated", "controlled_fake", "ephemeral_docker", "installation"
]


class _InventoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _CommandEntry(_InventoryModel):
    id: str
    document: str
    mode: AcceptanceMode
    evidence: str


class _InlineCommand(_InventoryModel):
    document: str
    command: str
    mode: AcceptanceMode
    evidence: str


class _CommandInventory(_InventoryModel):
    schema_version: Literal["sdi.repository-acceptance-command-inventory/v1"]
    commands: list[_CommandEntry]
    inline_commands: list[_InlineCommand]


def _inventory() -> _CommandInventory:
    loaded = YAML(typ="safe").load(  # pyright: ignore[reportUnknownMemberType]
        INVENTORY
    )
    return _CommandInventory.model_validate(loaded)


def _shell_fences(path: Path) -> list[tuple[str, str]]:
    fences: list[tuple[str, str]] = []
    for token in MarkdownIt("commonmark").parse(path.read_text(encoding="utf-8")):
        if token.type != "fence" or not token.info.startswith("sh"):
            continue
        attributes = dict(
            item.split("=", maxsplit=1) for item in token.info.split()[1:]
        )
        fences.append((attributes.get("acceptance", ""), token.content))
    return fences


def _inline_code(path: Path) -> set[str]:
    values: set[str] = set()
    for token in MarkdownIt("commonmark").parse(path.read_text(encoding="utf-8")):
        if token.children is None:
            continue
        values.update(
            child.content for child in token.children if child.type == "code_inline"
        )
    return values


def _evidence_exists(reference: str) -> bool:
    path_text, separator, symbol = reference.partition("::")
    path = (
        INTEGRATION_ROOT / path_text
        if path_text.startswith("tests/")
        else REPOSITORY_ROOT / path_text
    )
    if not path.is_file():
        return False
    return not separator or f"def {symbol}(" in path.read_text(encoding="utf-8")


def test_command_inventory_covers_every_guide_shell_example_and_reference() -> None:
    inventory = _inventory()
    assert inventory.schema_version == (
        "sdi.repository-acceptance-command-inventory/v1"
    )
    commands = inventory.commands
    by_id = {item.id: item for item in commands}
    assert len(by_id) == len(commands)

    observed: dict[str, str] = {}
    for document in GUIDES:
        for command_id, _content in _shell_fences(REPOSITORY_ROOT / document):
            assert command_id
            assert command_id not in observed
            observed[command_id] = document
    assert set(observed) == set(by_id)

    for command_id, item in by_id.items():
        assert observed[command_id] == item.document
        assert _evidence_exists(item.evidence)
        if item.mode == "installation":
            assert item.evidence == "docs/installation-readiness-checklist.md"

    inline_commands = inventory.inline_commands
    for item in inline_commands:
        assert item.command in _inline_code(REPOSITORY_ROOT / item.document)
        assert _evidence_exists(item.evidence)


def test_github_examples_target_one_exact_run(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "commands.log"
    for name in ("curl", "gh", "uv"):
        command = fake_bin / name
        command.write_text(
            '#!/bin/sh\nprintf \'%s %s\\n\' "${0##*/}" "$*" >> "$COMMAND_LOG"\n',
            encoding="utf-8",
        )
        command.chmod(0o755)

    fences = dict(
        _shell_fences(REPOSITORY_ROOT / "docs/pipeline-integration-trigger.md")
    )
    environment = os.environ | {
        "COMMAND_LOG": str(log),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    execution_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    command_ids = [
        item.id
        for item in _inventory().commands
        if item.document == "docs/pipeline-integration-trigger.md"
        and item.evidence.endswith("::test_github_examples_target_one_exact_run")
    ]
    for command_id in command_ids:
        command = (
            fences[command_id]
            .replace("<workflow-run-id>", "1000")
            .replace("<execution-id>", execution_id)
            .replace("<fine-grained-token>", "temporary-token")
        )
        completed = subprocess.run(
            ["sh", "-eu", "-c", command],
            check=False,
            capture_output=True,
            cwd=tmp_path,
            env=environment,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr

    entries = log.read_text(encoding="utf-8").splitlines()
    assert any(
        "workflow run pipeline-integration.yml --ref main --field "
        "run_request_path=runs/s-04/s-04-tc-03-c-01-fixture.yaml" in entry
        for entry in entries
    )
    assert all("last" not in entry.casefold() for entry in entries)
    exact_run_entries = [
        entry
        for entry in entries
        if any(
            operation in entry
            for operation in ("run watch", "run view", "run download", "run cancel")
        )
    ]
    assert len(exact_run_entries) == 4
    assert all("1000" in entry for entry in exact_run_entries)
    assert any(f"pipeline-integration-{execution_id}" in entry for entry in entries)
    repository_entries = [
        entry
        for entry in entries
        if "api.github.com/repos/" in entry
        or "--repository" in entry
        or "--repo" in entry
    ]
    assert len(repository_entries) == 5
    assert all("open-SDI/ci-cv-cd_framework" in entry for entry in repository_entries)


def test_runner_examples_form_one_supported_lifecycle(tmp_path: Path) -> None:
    runner = tmp_path / "deployment/github-runner/bin/runner"
    runner.parent.mkdir(parents=True)
    log = tmp_path / "runner.log"
    runner.write_text(
        "#!/bin/sh\n"
        "printf '%s|%s|%s|%s|%s\\n' \"$*\" "
        '"${SDI_GITHUB_RUNNER_REPOSITORY-}" "${SDI_GITHUB_RUNNER_NAME-}" '
        '"${SDI_GITHUB_HANDOFF_QUIESCED-}" '
        '"${SDI_GITHUB_RUNNER_REMOVAL_TOKEN-}" >> "$COMMAND_LOG"\n',
        encoding="utf-8",
    )
    runner.chmod(0o755)
    fences = dict(_shell_fences(REPOSITORY_ROOT / "deployment/github-runner/README.md"))
    environment = os.environ | {"COMMAND_LOG": str(log)}

    for item in _inventory().commands:
        if item.document != "deployment/github-runner/README.md":
            continue
        command = (
            fences[item.id]
            .replace("<short-lived-registration-token>", "registration-token")
            .replace("<short-lived-removal-token>", "removal-token")
        )
        completed = subprocess.run(
            ["sh", "-eu", "-c", command],
            check=False,
            capture_output=True,
            cwd=tmp_path,
            env=environment,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr

    entries = [line.split("|") for line in log.read_text(encoding="utf-8").splitlines()]
    assert [entry[0] for entry in entries] == [
        "preflight",
        "install",
        "configure",
        "install-service",
        "start",
        "status",
        "stop",
        "unregister",
        "remove-service",
        "remove",
    ]
    assert entries[2][1:3] == [
        "https://github.com/open-SDI/ci-cv-cd_framework",
        "jenkins-handoff-host",
    ]
    assert entries[7][3:] == ["true", "removal-token"]
    assert all(entry[3] == "true" for entry in entries[7:])
