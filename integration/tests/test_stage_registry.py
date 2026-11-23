"""Invariants of the Stage registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from sdi_pipeline_integration._stage_contracts import AdapterDescriptor
from sdi_pipeline_integration._stage_profiles import (
    stage_profile_bytes,
    stage_profile_file_name,
)
from sdi_pipeline_integration._stage_registry import (
    AGENTS,
    INTEGRATION_AGENT,
    STAGES,
    StageName,
)
from sdi_pipeline_integration._yaml_input import parse_yaml

REPOSITORY_ROOT = Path(__file__).parents[2]
FIX = "fix the row in _stage_registry.STAGES"


@pytest.mark.parametrize("name", STAGES)
def test_each_descriptor_validates_and_names_its_stage(name: StageName) -> None:
    stage = STAGES[name]
    assert stage.name == name, f"{name}: row is keyed under another name; {FIX}"
    for path in dict.fromkeys((stage.jenkins_descriptor, stage.local_descriptor)):
        descriptor = AdapterDescriptor.model_validate(
            parse_yaml((REPOSITORY_ROOT / path).read_bytes(), path)
        )
        assert descriptor.stage == name, f"{name}: {path} names {descriptor.stage}"


def test_agents_are_unique_and_each_stage_has_its_own() -> None:
    nodes = [agent.node for agent in AGENTS]
    labels = [agent.label for agent in AGENTS]
    assert len(set(nodes)) == len(nodes), f"agent nodes repeat: {nodes}; {FIX}"
    assert len(set(labels)) == len(labels), f"agent labels repeat: {labels}; {FIX}"
    stage_agents = [stage.agent for stage in STAGES.values()]
    assert INTEGRATION_AGENT not in stage_agents, (
        f"a Stage runs on the integration agent; {FIX}"
    )
    assert len(set(stage_agents)) == len(stage_agents), (
        f"two Stages share an agent: {stage_agents}; {FIX}"
    )


@pytest.mark.parametrize("name", STAGES)
def test_each_profile_renders_as_committed(name: StageName) -> None:
    committed = REPOSITORY_ROOT / "integration/stage-profiles"
    assert (committed / stage_profile_file_name(name)).read_bytes() == (
        stage_profile_bytes(name)
    ), f"{name}: profile is stale; run sdi-integration stage-profiles --write"
