"""Choice of the real generator and the keys the live tests use.

Qwen is self-hosted to keep live tests cheap, so they use it while its endpoint
serves its model and luna otherwise. The product never makes this choice; the
tests pass it as --generator. Discovery embeds with OpenAI for every generator,
so a qwen run still needs the OpenAI key.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from typing import cast

from sdi_pipeline_integration._yaml_input import parse_yaml

REPOSITORY_ROOT = Path(__file__).parents[2]
COMPOSITION_CONFIG = REPOSITORY_ROOT / "integration/composition-config.yaml"


def key(name: str) -> str | None:
    """Read a key from the environment, else from the repository's .env."""
    if value := os.environ.get(name):
        return value
    dotenv = REPOSITORY_ROOT / ".env"
    if not dotenv.is_file():
        return None
    for line in dotenv.read_text().splitlines():
        entry, _, value = line.partition("=")
        if entry.strip() == name:
            return value.strip().strip("\"'") or None
    return None


def openai_key() -> str | None:
    """Read the OpenAI key from the environment, else from the repository's .env."""
    return key("OPENAI_API_KEY")


def _config() -> dict[str, dict[str, dict[str, str]]]:
    return cast(
        "dict[str, dict[str, dict[str, str]]]",
        parse_yaml(COMPOSITION_CONFIG.read_bytes(), "composition-config.yaml"),
    )


def _serves(generator: dict[str, str]) -> bool:
    """Say whether the endpoint lists the generator's model.

    Requiring the model id keeps another service on the same port from passing.
    """
    api_key = key(generator["api_key_env"])
    if not api_key:
        return False
    request = urllib.request.Request(  # noqa: S310
        f"{generator['base_url']}/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            models = cast("dict[str, object]", json.load(response))
    except (OSError, ValueError):
        return False
    data = models.get("data")
    return isinstance(data, list) and any(
        isinstance(item, dict)
        and cast("dict[str, object]", item).get("id") == generator["model"]
        for item in cast("list[object]", data)
    )


def live_generator() -> str:
    """Pick qwen while its endpoint serves its model, else luna."""
    return "qwen" if _serves(_config()["generators"]["qwen"]) else "luna"


def live_keys(generator: str) -> dict[str, str] | None:
    """Return the generator's and the embeddings' keys, or None if one is missing."""
    config = _config()
    names = {
        config["generators"][generator]["api_key_env"],
        config["discovery"]["embeddings"]["api_key_env"],
    }
    keys = {name: key(name) for name in names}
    if not all(keys.values()):
        return None
    return cast("dict[str, str]", keys)


def lexical_only_config() -> str:
    """Return the committed config without embeddings.

    A generator's rejected key is then the run's first provider error.
    """
    text = COMPOSITION_CONFIG.read_text()
    return f"{text[: text.index('discovery:')]}discovery: {{}}\n"
