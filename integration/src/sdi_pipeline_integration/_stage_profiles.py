"""Committed Stage profiles rendered deterministically from the slot registry."""

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING

from ruamel.yaml import YAML

from ._generated_files import check_generated, write_generated
from ._stage_contracts import STAGE_GRANTS, render_stage_profile

if TYPE_CHECKING:
    from pathlib import Path

    from ._stage_contracts import StageName


def stage_profile_file_name(stage: StageName) -> str:
    """Name the committed profile file of one Stage."""
    return f"{stage.replace('_', '-')}-v1.yaml"


def stage_profile_bytes(stage: StageName) -> bytes:
    """Encode one Stage's rendered profile exactly as it is committed."""
    yaml = YAML(typ="safe", pure=True)
    yaml.default_flow_style = False
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    # Keep the contract's field order, which is the order profiles are read in.
    yaml.representer.sort_base_mapping_type_on_output = False
    stream = StringIO()
    data = render_stage_profile(stage).model_dump(mode="json")
    yaml.dump(data, stream)  # pyright: ignore[reportUnknownMemberType]
    return stream.getvalue().encode()


def _rendered_profiles() -> dict[str, bytes]:
    return {
        stage_profile_file_name(stage): stage_profile_bytes(stage)
        for stage in STAGE_GRANTS
    }


def write_stage_profiles(directory: Path) -> None:
    """Write every Stage profile rendered from the registry."""
    write_generated(directory, _rendered_profiles())


def check_stage_profiles(directory: Path) -> None:
    """Reject missing, obsolete, or hand-edited committed Stage profiles."""
    check_generated(
        directory,
        "*.yaml",
        _rendered_profiles(),
        singular="Stage profile",
        plural="Stage profiles",
    )
