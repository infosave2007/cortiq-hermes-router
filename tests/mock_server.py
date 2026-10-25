"""Local mock of OpenAI-compatible and Anthropic endpoints for tests (127.0.0.1 only).

The reply behaviour is chosen by the ``model`` id so one server covers the happy
path and each failure mode:

* ``qwen3:8b``      SSE stream, text "pong from local" (Ollama-style)
* ``plain-json``    plain JSON body despite stream=true
* ``tool-call``     SSE stream with a tool call split over several deltas
* ``reasoning``     SSE stream with reasoning_content and content
* ``http-500``      HTTP 500
* ``needs-auth``    HTTP 401 unless ``Authorization: Bearer <MOCK_KEY>``
* ``bad-json``      HTTP 200 with a body that is not JSON
* ``bad-stream``    SSE with a frame that is not JSON
* ``stream-error``  SSE with an error event
* ``empty``         SSE with only ``[DONE]``
* ``slow``          sleeps 1.5s before answering (exercises timeoutMs)
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MOCK_KEY = "mock-test-key-0001"
MODELS_LIST = {"object": "list", "data": [{"id": "qwen3:8b"}, {"id": "llama3.2"}]}


class _Handler(BaseHTTPRequestHandler):
    server: "MockServer"  # pyright: ignore[reportIncompatibleVariableOverride]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        return

    def _send(self, status: int, payload: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # The client gave up first (timeout test); nothing to report.
            pass

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data).encode("utf-8"))

    def _sse(self, events: list[str]) -> None:
        body = "".join(f"data: {event}\n\n" for event in events).encode("utf-8")
        self._send(200, body, "text/event-stream")

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        if self.path == "/v1/models":
            self.server.seen.append({"method": "GET", "path": self.path, "headers": _headers(self.headers)})
            self._json(200, MODELS_LIST)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            body = {}
        self.server.seen.append({"method": "POST", "path": self.path,
                                 "headers": _headers(self.headers), "body": body})
        if self.path == "/v1/messages":
            self._anthropic(body)
        elif self.path == "/v1/chat/completions":
            self._chat(body)
        else:
            self._json(404, {"error": "not found"})

    # --- OpenAI-compatible ------------------------------------------------- #

    def _chat(self, body: dict) -> None:
        model = str(body.get("model"))
        if model == "http-500":
            self._json(500, {"error": {"message": "boom"}})
        elif model == "needs-auth" and self.headers.get("Authorization") != f"Bearer {MOCK_KEY}":
            self._json(401, {"error": {"message": "invalid key"}})
        elif model == "bad-json":
            self._send(200, b"this is { not json")
        elif model == "bad-stream":
            self._send(200, b"data: {not json\n\n", "text/event-stream")
        elif model == "stream-error":
            self._sse([json.dumps({"error": {"message": "overloaded"}})])
        elif model == "empty":
            self._sse(["[DONE]"])
        elif model == "slow":
            time.sleep(1.5)
            self._json(200, _openai_json("pong from slow"))
        elif model == "plain-json":
            self._json(200, _openai_json("pong from plain json"))
        elif model == "tool-call":
            self._sse([
                _chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                        "function": {"name": "lookup", "arguments": "{\"q\":"}}]}),
                _chunk({"tool_calls": [{"index": 0, "function": {"arguments": "\"cats\"}"}}]}),
                _chunk({}, finish="tool_calls"),
                "[DONE]",
            ])
        elif model == "reasoning":
            self._sse([
                _chunk({"reasoning_content": "thinking…"}),
                _chunk({"content": "pong from reasoning"}),
                _chunk({}, finish="stop"),
                "[DONE]",
            ])
        else:
            self._sse([
                _chunk({"role": "assistant", "content": "pong "}),
                _chunk({"content": "from local"}),
                _chunk({}, finish="stop"),
                "[DONE]",
            ])

    # --- Anthropic Messages ------------------------------------------------ #

    def _anthropic(self, body: dict) -> None:
        if self.headers.get("x-api-key") != MOCK_KEY:
            self._json(401, {"type": "error", "error": {"type": "authentication_error"}})
            return
        if body.get("tools"):
            content = [{"type": "tool_use", "id": "toolu_1", "name": body["tools"][0]["name"],
                        "input": {"q": "cats"}}]
            stop = "tool_use"
        else:
            content = [{"type": "text", "text": "pong from anthropic"}]
            stop = "end_turn"
        self._json(200, {
            "id": "msg_1", "type": "message", "role": "assistant", "model": body.get("model"),
            "content": content, "stop_reason": stop,
            "usage": {"input_tokens": 11, "output_tokens": 5},
        })


def _headers(message) -> dict:
    """Header keys are matched case-insensitively (http.server re-capitalizes them)."""
    return {key.lower(): value for key, value in message.items()}


def _chunk(delta: dict, finish: Any = None) -> str:
    return json.dumps({
        "id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": "mock",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    })


def _openai_json(text: str) -> dict:
    return {
        "id": "chatcmpl-json", "object": "chat.completion", "model": "mock",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }


class MockServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.seen: list[dict[str, Any]] = []
        self.base_url = f"http://127.0.0.1:{self.server_address[1]}/v1"
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    def __enter__(self) -> "MockServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.shutdown()
        self.server_close()
