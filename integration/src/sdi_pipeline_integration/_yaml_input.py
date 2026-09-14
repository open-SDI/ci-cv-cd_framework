"""Strict YAML parsing for human-authored committed inputs."""

# pyright: reportUnknownArgumentType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING, Any, cast

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.nodes import MappingNode, ScalarNode, SequenceNode
from ruamel.yaml.tokens import (
    AliasToken,
    AnchorToken,
    BlockEndToken,
    BlockMappingStartToken,
    BlockSequenceStartToken,
    FlowMappingEndToken,
    FlowMappingStartToken,
    FlowSequenceEndToken,
    FlowSequenceStartToken,
)

if TYPE_CHECKING:
    from ruamel.yaml.nodes import Node

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_NESTING_DEPTH = 32
MAX_NODE_COUNT = 50_000
STANDARD_TAGS = {
    "tag:yaml.org,2002:map",
    "tag:yaml.org,2002:seq",
    "tag:yaml.org,2002:str",
    "tag:yaml.org,2002:null",
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:timestamp",
}
NESTING_START_TOKENS = (
    BlockMappingStartToken,
    BlockSequenceStartToken,
    FlowMappingStartToken,
    FlowSequenceStartToken,
)
NESTING_END_TOKENS = (BlockEndToken, FlowMappingEndToken, FlowSequenceEndToken)


class InputError(ValueError):
    """A safe validation error for an accepted input boundary."""


def _yaml() -> YAML:
    yaml = YAML(typ="safe", pure=True)
    yaml.allow_duplicate_keys = False
    yaml.version = (1, 2)
    return yaml


def _inspect_tokens(text: str, path: str) -> None:
    depth = 0
    token_count = 0
    try:
        tokens = _yaml().scan(StringIO(text))
        for token in tokens:
            token_count += 1
            if token_count > MAX_NODE_COUNT:
                msg = f"{path}: YAML contains more than {MAX_NODE_COUNT} tokens"
                raise InputError(msg)
            if isinstance(token, (AnchorToken, AliasToken)):
                msg = f"{path}: YAML anchors are not accepted"
                raise InputError(msg)
            if isinstance(token, NESTING_START_TOKENS):
                depth += 1
                if depth > MAX_NESTING_DEPTH:
                    msg = f"{path}: YAML nesting exceeds {MAX_NESTING_DEPTH} levels"
                    raise InputError(msg)
            elif isinstance(token, NESTING_END_TOKENS):
                depth -= 1
    except RecursionError as error:
        msg = f"{path}: YAML nesting exceeds {MAX_NESTING_DEPTH} levels"
        raise InputError(msg) from error
    except YAMLError as error:
        msg = f"{path}: invalid YAML: {error}"
        raise InputError(msg) from error


def _walk_node(  # noqa: C901
    node: Node,
    *,
    depth: int,
    seen: set[int],
    node_count: list[int],
) -> None:
    if depth > MAX_NESTING_DEPTH:
        msg = f"YAML nesting exceeds {MAX_NESTING_DEPTH} levels"
        raise InputError(msg)
    node_count[0] += 1
    if node_count[0] > MAX_NODE_COUNT:
        msg = f"YAML contains more than {MAX_NODE_COUNT} nodes"
        raise InputError(msg)
    identity = id(node)
    if identity in seen:
        msg = "YAML aliases are not accepted"
        raise InputError(msg)
    seen.add(identity)
    if node.tag not in STANDARD_TAGS:
        msg = f"YAML tag is not accepted: {node.tag}"
        raise InputError(msg)
    if getattr(node, "anchor", None) is not None:
        msg = "YAML anchors are not accepted"
        raise InputError(msg)

    if isinstance(node, MappingNode):
        for key, value in node.value:
            if not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str":
                msg = "YAML mapping keys must be strings"
                raise InputError(msg)
            if key.value == "<<":
                msg = "YAML merge keys are not accepted"
                raise InputError(msg)
            _walk_node(key, depth=depth + 1, seen=seen, node_count=node_count)
            _walk_node(value, depth=depth + 1, seen=seen, node_count=node_count)
    elif isinstance(node, SequenceNode):
        for value in node.value:
            _walk_node(value, depth=depth + 1, seen=seen, node_count=node_count)
    elif not isinstance(node, ScalarNode):
        msg = "unsupported YAML node"
        raise InputError(msg)


def parse_yaml(raw: bytes, path: str) -> dict[str, Any]:
    """Parse one bounded UTF-8 YAML mapping under the project policy."""
    if len(raw) > MAX_INPUT_BYTES:
        msg = f"{path}: file exceeds {MAX_INPUT_BYTES} bytes"
        raise InputError(msg)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        msg = f"{path}: file is not valid UTF-8"
        raise InputError(msg) from error

    _inspect_tokens(text, path)
    try:
        nodes = list(_yaml().compose_all(StringIO(text)))
    except (RecursionError, YAMLError) as error:
        msg = f"{path}: invalid YAML: {error}"
        raise InputError(msg) from error
    if len(nodes) != 1 or nodes[0] is None:
        msg = f"{path}: expected exactly one YAML document"
        raise InputError(msg)
    root = nodes[0]
    _walk_node(root, depth=1, seen=set(), node_count=[0])

    try:
        loaded = _yaml().load(text)
    except (RecursionError, YAMLError) as error:
        msg = f"{path}: invalid YAML: {error}"
        raise InputError(msg) from error
    if not isinstance(loaded, dict):
        msg = f"{path}: YAML document root must be a mapping"
        raise InputError(msg)
    return cast("dict[str, Any]", loaded)
