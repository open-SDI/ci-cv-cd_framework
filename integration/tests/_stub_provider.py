"""A local Responses endpoint for answers the real model cannot give on demand.

Live tests cover generated, exhausted and rejected-key outcomes against the real
provider. A refusal, a content-filter stop, output outside the proposal schema,
a failed response and a chosen set of proposals cannot be requested from it, so
these tests point a composition config's base_url at this stub, which answers
every Responses request with one recorded body. Embeddings requests get a small
deterministic vector per input text, so discovery's dense ranking runs without
a provider, or a server error for one named model. The real SDK still parses
every answer.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from collections.abc import Generator

STUB_KEY = "stub-only-key"
# The embeddings model the stub answers with a server error.
UNAVAILABLE_EMBEDDINGS = "stub/unavailable"


def _response(
    status: str,
    content: list[dict[str, object]],
    **fields: object,
) -> dict[str, object]:
    return {
        "id": "resp_stub",
        "object": "response",
        "created_at": 0,
        "model": "stub/model",
        "status": status,
        "error": None,
        "incomplete_details": None,
        "output": [
            {
                "type": "message",
                "id": "msg_stub",
                "role": "assistant",
                "status": "completed",
                "content": content,
            }
        ],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 1,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens": 1,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 2,
        },
        **fields,
    }


def _text(text: str) -> list[dict[str, object]]:
    return [{"type": "output_text", "text": text, "annotations": []}]


# Recorded bodies by the generation outcome each one produces.
BODIES: dict[str, dict[str, object]] = {
    "content-filter": _response(
        "incomplete", [], incomplete_details={"reason": "content_filter"}
    ),
    "refusal": _response(
        "completed", [{"type": "refusal", "refusal": "Test-only refusal."}]
    ),
    "off-schema": _response("completed", _text('{"proposals": []}')),
    "failed": _response(
        "failed",
        [],
        error={"code": "server_error", "message": "Test-only failure."},
    ),
}


def proposals_body(proposals: list[dict[str, object]]) -> dict[str, object]:
    """Return a completed answer carrying the given proposals."""
    return _response("completed", _text(json.dumps({"proposals": proposals})))


def _embeddings(request: dict[str, Any]) -> bytes:
    """Embed each text by its counts of a few mission words."""
    words = ("navigat", "face", "speech", "audio", "camera", "map")
    return json.dumps(
        {
            "object": "list",
            "model": request["model"],
            "data": [
                {
                    "object": "embedding",
                    "index": index,
                    "embedding": [1.0] + [float(text.count(word)) for word in words],
                }
                for index, text in enumerate(request["input"])
            ],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }
    ).encode()


class _Handler(BaseHTTPRequestHandler):
    body: ClassVar[bytes] = b""
    received: ClassVar[list[tuple[str, str]]] = []

    def do_POST(self) -> None:
        request = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.received.append((self.path, self.headers.get("Authorization", "")))
        status, body = 200, self.body
        if self.path.endswith("/embeddings"):
            embedding = json.loads(request)
            if embedding["model"] == UNAVAILABLE_EMBEDDINGS:
                status, body = 503, b'{"error": {"message": "unavailable"}}'
            else:
                body = _embeddings(embedding)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args


@contextmanager
def stub_provider(
    body: dict[str, object], received: list[tuple[str, str]] | None = None
) -> Generator[str]:
    """Serve one recorded body to every request; yield the base_url.

    Each request's path and Authorization header are appended to received.
    """
    handler = type(
        "Handler",
        (_Handler,),
        {
            "body": json.dumps(body).encode(),
            "received": [] if received is None else received,
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()


def stub_config(
    base_url: str,
    *,
    max_context_bytes: int = 98304,
    embeddings: dict[str, str] | None = None,
    generators: str = "",
) -> str:
    """Return a composition config whose default generator, stub, is the stub.

    Embeddings settings, when given, override the stub's embeddings endpoint;
    generators adds further generator entries.
    """
    discovery = "discovery: {}\n"
    if embeddings is not None:
        settings = {
            "model": "stub/embeddings",
            "base_url": base_url,
            "api_key_env": "VLLM_KEY",
        } | embeddings
        discovery = "discovery:\n  embeddings:\n" + "".join(
            f"    {name}: {value}\n" for name, value in settings.items()
        )
    return (
        "schema_version: sdi.composition-config/v1\n"
        "default_generator: stub\n"
        "generators:\n"
        "  stub:\n"
        "    model: stub/model\n"
        "    reasoning_effort: low\n"
        "    max_output_tokens: 1000\n"
        "    timeout_seconds: 30\n"
        f"    max_context_bytes: {max_context_bytes}\n"
        f"    base_url: {base_url}\n"
        "    api_key_env: VLLM_KEY\n"
        f"{generators}{discovery}"
    )
