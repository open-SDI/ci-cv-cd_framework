"""Every Domain adapter entry point is built by the shared adapter kit."""

from __future__ import annotations

import tomllib
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from sdi_pipeline_integration._adapter_kit import DomainAdapter
from sdi_pipeline_integration._stage_contracts import AdapterDescriptor
from sdi_pipeline_integration._yaml_input import parse_yaml

REPOSITORY_ROOT = Path(__file__).parents[2]
FIX = (
    "Domain adapters define main = serve(stage, body) from _adapter_kit; "
    "copy _image_build_adapter.py"
)
FIXTURE_ADAPTER = "sdi-fixture-adapter"
SCRIPTS: dict[str, str] = tomllib.loads(
    (REPOSITORY_ROOT / "integration/pyproject.toml").read_text()
)["project"]["scripts"]
DOMAIN_SCRIPTS = sorted(
    name for name in SCRIPTS if name.endswith("-adapter") and name != FIXTURE_ADAPTER
)
DESCRIPTORS = sorted((REPOSITORY_ROOT / "deployment/jenkins/adapters").glob("*.yaml"))


def _main(script: str) -> object:
    (entry_point,) = entry_points(group="console_scripts", name=script)
    return entry_point.load()


@pytest.mark.parametrize("script", DOMAIN_SCRIPTS)
def test_domain_adapter_entry_points_are_served_by_the_kit(script: str) -> None:
    assert isinstance(_main(script), DomainAdapter), f"{script}: {FIX}"


@pytest.mark.parametrize("path", DESCRIPTORS, ids=lambda path: path.name)
def test_implemented_descriptors_run_an_adapter_served_for_their_stage(
    path: Path,
) -> None:
    descriptor = AdapterDescriptor.model_validate(
        parse_yaml(path.read_bytes(), str(path))
    )
    if descriptor.implementation_mode != "implemented":
        assert descriptor.entrypoint == FIXTURE_ADAPTER
        return
    main = _main(descriptor.entrypoint)
    assert isinstance(main, DomainAdapter), f"{descriptor.entrypoint}: {FIX}"
    assert main.stage == descriptor.stage, (
        f"{path.name} selects {descriptor.entrypoint}, which serves {main.stage}"
    )
