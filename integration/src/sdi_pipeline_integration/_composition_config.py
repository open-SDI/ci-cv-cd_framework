"""The committed composition config and the settings one run resolves from it.

One config names every generator and the discovery settings they share, so
every generator searches the same way. Each generator sets its own context
budget, which bounds what discovery selects. A run uses the config's default
generator unless the operator names another; nothing probes an endpoint or
falls back to a different generator.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator
from pydantic.json_schema import SkipJsonSchema  # noqa: TC002

from ._contracts import ContractModel, NonBlank, Slug, reject_explicit_nulls
from ._discovery import EmbeddingsConfig  # noqa: TC001
from ._generation import MAX_RETRIES, GeneratorConfig
from ._stage_contracts import Sha256  # noqa: TC001
from ._yaml_input import InputError, parse_yaml, validate_contract

DEFAULT_COMPOSITION_CONFIG = "integration/composition-config.yaml"

GeneratorName = Slug


class DiscoverySettings(ContractModel):
    """Discovery settings every generator shares; no embeddings is lexical only."""

    embeddings: EmbeddingsConfig | SkipJsonSchema[None] = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_unknowns(cls, data: object) -> object:
        """Require absent embeddings to be omitted rather than null."""
        return reject_explicit_nulls(data, ("embeddings",))


class CompositionConfig(ContractModel):
    """Every named generator, the default one and the shared discovery settings."""

    schema_version: Literal["sdi.composition-config/v1"]
    default_generator: GeneratorName
    generators: Annotated[dict[GeneratorName, GeneratorConfig], Field(min_length=1)]
    discovery: DiscoverySettings

    @model_validator(mode="after")
    def require_defined_default(self) -> Self:
        if self.default_generator not in self.generators:
            msg = "default_generator must name a defined generator"
            raise ValueError(msg)
        return self


class CompositionSettings(ContractModel):
    """The one generator a run uses, with the shared discovery settings.

    The config's path and digest tie these settings to the committed bytes they
    were resolved from.
    """

    schema_version: Literal["sdi.composition-settings/v1"]
    config_path: NonBlank
    config_sha256: Sha256
    generator: GeneratorName
    generator_config: GeneratorConfig
    discovery: DiscoverySettings

    @property
    def key_names(self) -> tuple[str, ...]:
        """Name the environment variables holding the keys this run uses."""
        names = [self.generator_config.api_key_env]
        if self.discovery.embeddings is not None:
            names.append(self.discovery.embeddings.api_key_env)
        return tuple(dict.fromkeys(names))

    def evidence_settings(self) -> dict[str, object]:
        """Return the settings evidence records; key names stay out of it."""
        return {
            "composition_config_sha256": self.config_sha256,
            "generator": self.generator,
            **self.generator_config.model_dump(exclude={"api_key_env"}),
            "max_retries": MAX_RETRIES,
        }


def resolve_composition_settings(
    content: bytes, *, path: str, generator: str | None
) -> CompositionSettings:
    """Resolve a config's bytes to the named generator, or its default."""
    config = validate_contract(CompositionConfig, parse_yaml(content, path), path)
    selected = config.default_generator if generator is None else generator
    if selected not in config.generators:
        known = ", ".join(sorted(config.generators))
        msg = f"unknown generator {selected!r}; the config defines {known}"
        raise InputError(msg)
    return CompositionSettings(
        schema_version="sdi.composition-settings/v1",
        config_path=path,
        config_sha256=hashlib.sha256(content).hexdigest(),
        generator=selected,
        generator_config=config.generators[selected],
        discovery=config.discovery,
    )
