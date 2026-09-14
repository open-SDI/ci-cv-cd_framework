"""Strict bounded JSON parsing for machine-produced records."""

from __future__ import annotations

import json
from typing import Any, cast

from ._yaml_input import InputError

MAX_JSON_DEPTH = 32


def _reject_constant(value: str) -> None:
    msg = f"non-finite JSON number is not accepted: {value}"
    raise InputError(msg)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            msg = f"duplicate JSON key is not accepted: {key}"
            raise InputError(msg)
        result[key] = value
    return result


def _check_depth(value: object, depth: int = 1) -> None:
    if depth > MAX_JSON_DEPTH:
        msg = f"JSON nesting exceeds {MAX_JSON_DEPTH} levels"
        raise InputError(msg)
    if isinstance(value, dict):
        for nested in cast("dict[object, object]", value).values():
            _check_depth(nested, depth + 1)
    elif isinstance(value, list):
        for nested in cast("list[object]", value):
            _check_depth(nested, depth + 1)


def parse_json(raw: bytes, path: str, *, max_bytes: int) -> dict[str, Any]:
    """Parse one bounded strict UTF-8 JSON object."""
    if len(raw) > max_bytes:
        msg = f"{path}: file exceeds {max_bytes} bytes"
        raise InputError(msg)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        msg = f"{path}: file is not valid UTF-8"
        raise InputError(msg) from error
    try:
        loaded = json.loads(
            text,
            object_pairs_hook=_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, RecursionError) as error:
        msg = f"{path}: invalid JSON: {error}"
        raise InputError(msg) from error
    if not isinstance(loaded, dict):
        msg = f"{path}: JSON document root must be an object"
        raise InputError(msg)
    _check_depth(cast("object", loaded))
    return cast("dict[str, Any]", loaded)
