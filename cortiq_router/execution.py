"""Direct provider execution for object tier entries (the ``llm_execution`` path).

Only object entries (``{"provider", "model", ...}``) are executed here. String
entries keep going through Hermes' own OpenRouter client, unchanged.

Credentials: the value of the environment variable named by ``apiKeyEnv`` is read
at call time, placed in the request header and never stored, logged or returned.
Every failure is raised as :class:`ExecutionError` with a short machine reason
(``timeout``, ``connection``, ``http_500``, ``invalid_json``, ...) so the caller
can fail open and record the reason in statistics.
"""

from __future__ import annotations

import copy
import json
import os
import socket
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Any, Mapping, Optional

ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_DEFAULT_MAX_TOKENS = 4096

#: Hermes-side request keys that belong to the OpenAI SDK or to OpenRouter-only
#: routing knobs. Direct endpoints get the plain OpenAI-compatible body; anything
#: an endpoint must accept explicitly goes into the entry's ``extraBody``.
_SDK_ONLY_KEYS = frozenset({
    "extra_body", "extra_headers", "extra_query", "timeout", "stream", "stream_options",
})

# Responses-only request fields must never leak to an OpenAI Chat Completions
# endpoint.  Direct endpoint execution translates them before the HTTP boundary.
_RESPONSES_ONLY_KEYS = frozenset({
    "instructions", "input", "reasoning", "include", "max_output_tokens",
    "prompt_cache_key", "store", "background", "metadata", "previous_response_id",
    "truncation", "text", "parallel_tool_calls", "conversation",
})

_STOP_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
}


class ExecutionError(Exception):
    """A direct call failed; ``reason`` is a stable, secret-free machine code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------- #
# Response objects (attribute-style, matching what Hermes' turn loop reads).
# --------------------------------------------------------------------------- #

def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _usage(prompt: Any = 0, completion: Any = 0) -> SimpleNamespace:
    p = int(prompt or 0) if isinstance(prompt, (int, float)) else 0
    c = int(completion or 0) if isinstance(completion, (int, float)) else 0
    return SimpleNamespace(prompt_tokens=p, completion_tokens=c, total_tokens=p + c)


def make_response(*, response_id: str, model: str, content: Optional[str],
                  reasoning: Optional[str], tool_calls: list, finish_reason: Optional[str],
                  prompt_tokens: Any = 0, completion_tokens: Any = 0) -> SimpleNamespace:
    """Build a ChatCompletion-shaped object; fails if the endpoint produced nothing.

    An explicitly delivered ``finish_reason`` (``stop``, ``length``, ...) is a real
    termination signal and is kept even when the message body is empty. A reply
    that never signalled termination is empty only when it also carries no text,
    reasoning or tool call; otherwise it is treated as a completed ``stop``.
    """
    if not content and not reasoning and not tool_calls and not finish_reason:
        raise ExecutionError("empty_response", "endpoint returned no content")
    # ``""`` would silently become a legitimate stop signal; only a delivered
    # finish_reason counts, so an unsignalled empty reply stays empty_response.
    message = SimpleNamespace(
        role="assistant",
        content=content,
        tool_calls=tool_calls or None,
        reasoning_content=reasoning or None,
    )
    return SimpleNamespace(
        id=response_id or "",
        object="chat.completion",
        model=model or "",
        choices=[SimpleNamespace(index=0, message=message,
                                 finish_reason=finish_reason or "stop")],
        usage=_usage(prompt_tokens, completion_tokens),
    )


def _tool_call(raw: Mapping[str, Any]) -> SimpleNamespace:
    fn = raw.get("function") or {}
    arguments = fn.get("arguments")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
    return SimpleNamespace(
        id=str(raw.get("id") or ""),
        type="function",
        function=SimpleNamespace(name=str(fn.get("name") or ""), arguments=arguments),
    )


# --------------------------------------------------------------------------- #
# Credentials and request builders.
# --------------------------------------------------------------------------- #

def resolve_api_key(entry: Mapping[str, Any], env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Return the key named by ``apiKeyEnv`` (``None`` when the entry needs none)."""
    name = entry.get("apiKeyEnv")
    if not name:
        return None
    source = os.environ if env is None else env
    value = source.get(name)
    if not value:
        raise ExecutionError("missing_key", f"${name} is not set")
    return value


def _url(base: str, path: str) -> str:
    return base.rstrip("/") + path


def _json_bytes(body: Any) -> bytes:
    try:
        return json.dumps(body, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExecutionError("unsupported_request", f"request is not JSON-serializable: {exc}") from None


def is_responses_request(request: Mapping[str, Any]) -> bool:
    """True for Hermes' Responses/Codex payload shape, not merely a response option."""
    return "input" in request and "messages" not in request


def _responses_content_to_chat(content: Any) -> Any:
    """Responses message content → OpenAI Chat Completions content."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if not isinstance(content, list):
        raise ExecutionError("unsupported_request", "Responses message content must be text or a list")
    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            raise ExecutionError("unsupported_request", "Responses content part must be an object")
        kind = part.get("type")
        if kind in ("input_text", "output_text", "text"):
            parts.append({"type": "text", "text": str(part.get("text") or "")})
        elif kind == "input_image":
            image = part.get("image_url")
            if isinstance(image, str):
                image = {"url": image}
            if not isinstance(image, dict) or not isinstance(image.get("url"), str):
                raise ExecutionError("unsupported_request", "Responses input_image has no image_url")
            converted = {"type": "image_url", "image_url": {"url": image["url"]}}
            if image.get("detail") is not None:
                converted["image_url"]["detail"] = image["detail"]
            parts.append(converted)
        else:
            raise ExecutionError("unsupported_request", f"Responses content part '{kind}' is not supported")
    if all(part["type"] == "text" for part in parts):
        return "\n".join(str(part["text"]) for part in parts)
    return parts


def _responses_output_to_text(output: Any) -> str:
    content = _responses_content_to_chat(output)
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(part.get("type") == "text" for part in content):
        return "\n".join(str(part.get("text") or "") for part in content)
    raise ExecutionError("unsupported_request", "non-text function_call_output is not supported")


def _responses_tools_to_chat(tools: Any) -> list[dict[str, Any]]:
    if tools is None:
        return []
    if not isinstance(tools, list):
        raise ExecutionError("unsupported_request", "Responses tools must be a list")
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("name"), str):
            raise ExecutionError("unsupported_request", "only Responses function tools are supported")
        function = {
            "name": tool["name"],
            "description": str(tool.get("description") or ""),
            "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
        }
        if isinstance(tool.get("strict"), bool):
            function["strict"] = tool["strict"]
        converted.append({"type": "function", "function": function})
    return converted


def _responses_tool_choice_to_chat(choice: Any) -> Any:
    if choice in (None, "auto", "required", "none"):
        return choice
    if isinstance(choice, dict) and choice.get("type") == "function" and isinstance(choice.get("name"), str):
        return {"type": "function", "function": {"name": choice["name"]}}
    raise ExecutionError("unsupported_request", "Responses tool_choice is not supported")


def responses_to_chat_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Losslessly map the supported Responses subset to Chat Completions.

    Encrypted reasoning and compaction items belong to the issuing Responses
    backend and cannot be replayed to a Chat Completions endpoint; they are
    deliberately dropped.  Unknown item types fail open instead of crossing the
    provider boundary with a malformed request.
    """
    raw_input = request.get("input")
    if not isinstance(raw_input, list):
        raise ExecutionError("unsupported_request", "Responses input must be a list")
    messages: list[dict[str, Any]] = []
    instructions = request.get("instructions")
    if instructions is not None:
        if not isinstance(instructions, str):
            raise ExecutionError("unsupported_request", "Responses instructions must be text")
        if instructions:
            messages.append({"role": "system", "content": instructions})
    for item in raw_input:
        if not isinstance(item, dict):
            raise ExecutionError("unsupported_request", "Responses input item must be an object")
        kind = item.get("type")
        if kind == "message":
            role = item.get("role")
            if role not in ("system", "developer", "user", "assistant"):
                raise ExecutionError("unsupported_request", f"Responses message role '{role}' is not supported")
            # Chat endpoints consistently support system; developer is Responses-specific.
            messages.append({"role": "system" if role == "developer" else role,
                             "content": _responses_content_to_chat(item.get("content"))})
        elif kind == "function_call":
            name = item.get("name")
            call_id = item.get("call_id") or item.get("id")
            if not isinstance(name, str) or not name or not isinstance(call_id, str) or not call_id:
                raise ExecutionError("unsupported_request", "Responses function_call lacks name or call_id")
            arguments = item.get("arguments", "{}")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            messages.append({"role": "assistant", "content": None, "tool_calls": [{
                "id": call_id, "type": "function", "function": {"name": name, "arguments": arguments},
            }]})
        elif kind == "function_call_output":
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise ExecutionError("unsupported_request", "Responses function_call_output lacks call_id")
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": _responses_output_to_text(item.get("output"))})
        elif kind in ("reasoning", "compaction"):
            continue
        else:
            raise ExecutionError("unsupported_request", f"Responses input item '{kind}' is not supported")
    if not messages:
        raise ExecutionError("unsupported_request", "Responses input has no transferable messages")
    body: dict[str, Any] = {"messages": messages}
    if request.get("max_output_tokens") is not None:
        body["max_tokens"] = request["max_output_tokens"]
    for key in ("temperature", "top_p", "stop"):
        if request.get(key) is not None:
            body[key] = request[key]
    tools = _responses_tools_to_chat(request.get("tools"))
    if tools:
        body["tools"] = tools
        choice = _responses_tool_choice_to_chat(request.get("tool_choice"))
        if choice is not None:
            body["tool_choice"] = choice
    return body


def build_openai_request(entry: Mapping[str, Any], request: Mapping[str, Any],
                         api_key: Optional[str]) -> tuple[str, dict, bytes]:
    """OpenAI chat-completions body for ``baseUrl`` (Ollama, LM Studio, vLLM, llama.cpp, CMF, cloud)."""
    body = (responses_to_chat_request(request) if is_responses_request(request)
            else {key: value for key, value in request.items() if key not in _SDK_ONLY_KEYS})
    # Defense-in-depth: no Responses-only key can reach a Chat Completions endpoint.
    body = {key: value for key, value in body.items() if key not in _RESPONSES_ONLY_KEYS}
    body.update(copy.deepcopy(entry.get("extraBody") or {}))
    body["model"] = entry["model"]
    # Hermes streams by default; the upstream is always asked for SSE and the
    # reply is assembled here (plain JSON replies are accepted too).
    body["stream"] = True
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream, application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return _url(entry["baseUrl"], "/chat/completions"), headers, _json_bytes(body)


def _text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                raise ExecutionError("unsupported_request", "unsupported content part")
            kind = part.get("type")
            if kind in ("text", "input_text"):
                parts.append(str(part.get("text", "")))
            else:
                raise ExecutionError("unsupported_request", f"content part '{kind}' is not supported")
        return "\n".join(parts)
    raise ExecutionError("unsupported_request", "unsupported message content")


def to_anthropic_messages(messages: Any) -> tuple[Optional[str], list[dict]]:
    """OpenAI-style messages -> (system, Anthropic messages). Tool results become tool_result blocks."""
    if not isinstance(messages, list):
        raise ExecutionError("unsupported_request", "messages must be a list")
    system_parts: list[str] = []
    out: list[dict] = []

    def add(role: str, blocks: list[dict]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:  # Anthropic wants alternating turns
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for message in messages:
        if not isinstance(message, dict):
            raise ExecutionError("unsupported_request", "message must be an object")
        role = message.get("role")
        if role in ("system", "developer"):
            text = _text_of(message.get("content"))
            if text:
                system_parts.append(text)
        elif role == "user":
            text = _text_of(message.get("content"))
            add("user", [{"type": "text", "text": text}] if text else [])
        elif role == "assistant":
            blocks: list[dict] = []
            text = _text_of(message.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for call in message.get("tool_calls") or []:
                fn = (call or {}).get("function") or {}
                raw_args = fn.get("arguments") or "{}"
                try:
                    parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except ValueError:
                    parsed = {}
                blocks.append({
                    "type": "tool_use",
                    "id": str((call or {}).get("id") or ""),
                    "name": str(fn.get("name") or ""),
                    "input": parsed if isinstance(parsed, dict) else {},
                })
            add("assistant", blocks)
        elif role == "tool":
            add("user", [{
                "type": "tool_result",
                "tool_use_id": str(message.get("tool_call_id") or ""),
                "content": _text_of(message.get("content")),
            }])
        else:
            raise ExecutionError("unsupported_request", f"unsupported message role '{role}'")
    return ("\n\n".join(system_parts) or None), out


def _anthropic_tools(tools: Any) -> list[dict]:
    converted: list[dict] = []
    for tool in tools or []:
        if not isinstance(tool, dict) or tool.get("type", "function") != "function":
            continue
        fn = tool.get("function") or {}
        converted.append({
            "name": str(fn.get("name") or ""),
            "description": str(fn.get("description") or ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return converted


def _anthropic_tool_choice(choice: Any) -> Optional[dict]:
    if choice in (None, "auto"):
        return {"type": "auto"} if choice else None
    if choice == "required":
        return {"type": "any"}
    if choice == "none":
        return {"type": "none"}
    if isinstance(choice, dict) and isinstance(choice.get("function"), dict):
        return {"type": "tool", "name": str(choice["function"].get("name") or "")}
    return None


def build_anthropic_request(entry: Mapping[str, Any], request: Mapping[str, Any],
                            api_key: Optional[str]) -> tuple[str, dict, bytes]:
    request = responses_to_chat_request(request) if is_responses_request(request) else request
    system, messages = to_anthropic_messages(request.get("messages"))
    body: dict[str, Any] = {
        "model": entry["model"],
        "messages": messages,
        "max_tokens": int(request.get("max_tokens") or request.get("max_completion_tokens")
                          or ANTHROPIC_DEFAULT_MAX_TOKENS),
    }
    if system:
        body["system"] = system
    tools = _anthropic_tools(request.get("tools"))
    if tools:
        body["tools"] = tools
        choice = _anthropic_tool_choice(request.get("tool_choice"))
        if choice:
            body["tool_choice"] = choice
    for key in ("temperature", "top_p"):
        if request.get(key) is not None:
            body[key] = request[key]
    stop = request.get("stop")
    if stop:
        body["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    body.update(copy.deepcopy(entry.get("extraBody") or {}))
    body["model"] = entry["model"]
    headers = {"Content-Type": "application/json", "anthropic-version": ANTHROPIC_VERSION}
    if api_key:
        headers["x-api-key"] = api_key
    base = entry["baseUrl"].rstrip("/")
    path = "/messages" if base.endswith("/v1") else "/v1/messages"
    return _url(base, path), headers, _json_bytes(body)


def parse_anthropic(data: Any, requested_model: str) -> SimpleNamespace:
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        raise ExecutionError("invalid_response", "Anthropic reply has no content blocks")
    text_parts: list[str] = []
    tool_calls: list[SimpleNamespace] = []
    for block in data["content"]:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(str(block.get("text", "")))
        elif block.get("type") == "tool_use":
            tool_calls.append(SimpleNamespace(
                id=str(block.get("id") or ""),
                type="function",
                function=SimpleNamespace(
                    name=str(block.get("name") or ""),
                    arguments=json.dumps(block.get("input") or {}, ensure_ascii=False),
                ),
            ))
    usage = _dict(data.get("usage"))
    return make_response(
        response_id=str(data.get("id") or ""),
        model=str(data.get("model") or requested_model),
        content="".join(text_parts) or None,
        reasoning=None,
        tool_calls=tool_calls,
        finish_reason=_STOP_REASONS.get(data.get("stop_reason"), "stop"),
        prompt_tokens=usage.get("input_tokens"),
        completion_tokens=usage.get("output_tokens"),
    )


def parse_openai_json(data: Any, requested_model: str) -> SimpleNamespace:
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ExecutionError("invalid_response", "reply has no choices")
    choice = choices[0]
    message = _dict(choice.get("message"))
    tool_calls = [_tool_call(call) for call in message.get("tool_calls") or [] if isinstance(call, dict)]
    usage = _dict(data.get("usage"))
    delivered = choice.get("finish_reason")
    return make_response(
        response_id=str(data.get("id") or ""),
        model=str(data.get("model") or requested_model),
        content=message.get("content") if isinstance(message.get("content"), str) else None,
        reasoning=message.get("reasoning_content") or message.get("reasoning"),
        tool_calls=tool_calls,
        # Only a delivered finish_reason is a termination signal; defaulting to "stop"
        # here would hide an empty reply behind a plausible-looking stop.
        finish_reason=delivered or ("tool_calls" if tool_calls else None),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
    )


def chat_to_responses_response(reply: SimpleNamespace) -> SimpleNamespace:
    """Adapt a direct Chat Completions reply to the raw Responses shape Hermes normalizes."""
    choice = reply.choices[0]
    message = choice.message
    output: list[SimpleNamespace] = []
    reasoning = getattr(message, "reasoning_content", None)
    if isinstance(reasoning, str) and reasoning:
        output.append(SimpleNamespace(
            type="reasoning", summary=[SimpleNamespace(type="summary_text", text=reasoning)],
        ))
    content = getattr(message, "content", None)
    if isinstance(content, str) and content:
        output.append(SimpleNamespace(
            type="message", role="assistant", status="completed",
            content=[SimpleNamespace(type="output_text", text=content)],
        ))
    for index, call in enumerate(getattr(message, "tool_calls", None) or []):
        call_id = str(getattr(call, "id", "") or f"call_{index}")
        function = getattr(call, "function", None)
        output.append(SimpleNamespace(
            type="function_call", id=call_id, call_id=call_id,
            name=str(getattr(function, "name", "") or ""),
            arguments=str(getattr(function, "arguments", "{}") or "{}"),
        ))
    if not output:
        raise ExecutionError("empty_response", "direct reply cannot be adapted to Responses")
    return SimpleNamespace(
        id=getattr(reply, "id", ""), object="response", model=getattr(reply, "model", ""),
        status="completed", output=output, usage=getattr(reply, "usage", None),
    )


def native_text_response(*, text: Any, model: Any, usage: Any, request: Mapping[str, Any]) -> SimpleNamespace:
    """Host-native text completion adapted to the original request transport."""
    if not isinstance(text, str) or not text:
        raise ExecutionError("native_invalid_response", "Hermes native provider returned no text")
    chat = make_response(
        response_id="", model=str(model or ""), content=text, reasoning=None, tool_calls=[],
        finish_reason="stop", prompt_tokens=getattr(usage, "input_tokens", 0),
        completion_tokens=getattr(usage, "output_tokens", 0),
    )
    return chat_to_responses_response(chat) if is_responses_request(request) else chat


class _StreamAccumulator:
    """Folds OpenAI ``chat.completion.chunk`` events into one message."""

    def __init__(self, requested_model: str) -> None:
        self.model = requested_model
        self.response_id = ""
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.calls: dict[int, dict[str, Any]] = {}
        self.finish: Optional[str] = None
        self.usage: dict[str, Any] = {}

    def feed(self, event: Any) -> None:
        if not isinstance(event, dict):
            raise ExecutionError("invalid_stream", "stream event is not an object")
        if "error" in event:
            raise ExecutionError("stream_error", "endpoint reported an error inside the stream")
        self.response_id = str(event.get("id") or self.response_id)
        self.model = str(event.get("model") or self.model)
        self.usage = _dict(event.get("usage")) or self.usage
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return
        choice = choices[0]
        delta = _dict(choice.get("delta"))
        if isinstance(delta.get("content"), str):
            self.content.append(delta["content"])
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str):
            self.reasoning.append(reasoning)
        for call in delta.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            index = call.get("index", len(self.calls))
            slot = self.calls.setdefault(index, {"id": "", "name": [], "arguments": []})
            slot["id"] = str(call.get("id") or slot["id"])
            fn = _dict(call.get("function"))
            if fn.get("name"):
                slot["name"].append(str(fn["name"]))
            if isinstance(fn.get("arguments"), str):
                slot["arguments"].append(fn["arguments"])
        if choice.get("finish_reason"):
            self.finish = str(choice["finish_reason"])

    def result(self) -> SimpleNamespace:
        tool_calls = [
            _tool_call({"id": slot["id"], "function": {
                "name": "".join(slot["name"]), "arguments": "".join(slot["arguments"]) or "{}"}})
            for _index, slot in sorted(self.calls.items())
        ]
        if self.finish:
            finish = self.finish
        elif tool_calls:
            finish = "tool_calls"
        elif self.content:
            finish = "stop"  # text arrived but the endpoint never sent a finish frame
        else:
            finish = None  # unsignalled and empty -> make_response raises empty_response
        return make_response(
            response_id=self.response_id,
            model=self.model,
            content="".join(self.content) or None,
            reasoning="".join(self.reasoning) or None,
            tool_calls=tool_calls,
            finish_reason=finish,
            prompt_tokens=self.usage.get("prompt_tokens"),
            completion_tokens=self.usage.get("completion_tokens"),
        )


# --------------------------------------------------------------------------- #
# Transport.
# --------------------------------------------------------------------------- #

def _open(url: str, headers: dict, payload: Optional[bytes], timeout: float, method: str = "POST"):
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - scheme validated in config
    except urllib.error.HTTPError as exc:
        exc.close()
        raise ExecutionError(f"http_{exc.code}", f"HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise ExecutionError("timeout", "no response before timeoutMs") from None
        raise ExecutionError("connection", type(exc.reason).__name__) from None
    except (socket.timeout, TimeoutError):
        raise ExecutionError("timeout", "no response before timeoutMs") from None
    except OSError as exc:
        raise ExecutionError("connection", type(exc).__name__) from None


def _read_json(response) -> Any:
    try:
        raw = response.read()
    except (socket.timeout, TimeoutError):
        raise ExecutionError("timeout", "body read timed out") from None
    except OSError as exc:
        raise ExecutionError("connection", type(exc).__name__) from None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ExecutionError("invalid_json", "reply is not valid JSON") from None


def _read_openai_reply(response, requested_model: str) -> SimpleNamespace:
    """Accept an SSE stream (Hermes streams by default) or a plain JSON body."""
    accumulator = _StreamAccumulator(requested_model)
    try:
        first = b""
        for raw in response:
            if raw.strip():
                first = raw
                break
        if not first.strip():
            raise ExecutionError("empty_response", "endpoint sent an empty body")
        head = first.decode("utf-8", "replace").strip()
        if not head.startswith(("data:", "event:", ":")):
            body = first + b"".join(response)
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise ExecutionError("invalid_json", "reply is not valid JSON") from None
            return parse_openai_json(data, requested_model)

        def consume(line: str) -> bool:
            """Feed one SSE line; True once the terminal ``[DONE]`` arrives."""
            if not line or line.startswith((":", "event:")):
                return False
            if not line.startswith("data:"):
                raise ExecutionError("invalid_stream", "unexpected SSE line")
            payload = line[5:].strip()
            if payload == "[DONE]":
                return True
            try:
                event = json.loads(payload)
            except ValueError:
                raise ExecutionError("invalid_stream", "stream frame is not valid JSON") from None
            accumulator.feed(event)
            return False

        if consume(head):
            return accumulator.result()
        for raw in response:
            if consume(raw.decode("utf-8", "replace").strip()):
                break
    except (socket.timeout, TimeoutError):
        raise ExecutionError("timeout", "no bytes before timeoutMs") from None
    except OSError as exc:
        raise ExecutionError("connection", type(exc).__name__) from None
    return accumulator.result()


def call_endpoint(entry: Mapping[str, Any], request: Mapping[str, Any],
                  env: Optional[Mapping[str, str]] = None) -> SimpleNamespace:
    """Execute one direct endpoint call; adapt Replies to the active request wire shape."""
    if not entry.get("baseUrl"):
        raise ExecutionError("unsupported_request", "entry has no baseUrl")
    responses_mode = is_responses_request(request)
    api_key = resolve_api_key(entry, env)
    timeout = max(float(entry.get("timeoutMs") or 60000) / 1000.0, 0.001)
    if entry.get("apiMode") == "anthropic_messages":
        url, headers, payload = build_anthropic_request(entry, request, api_key)
        response = _open(url, headers, payload, timeout)
        try:
            reply = parse_anthropic(_read_json(response), entry["model"])
            return chat_to_responses_response(reply) if responses_mode else reply
        finally:
            response.close()
    url, headers, payload = build_openai_request(entry, request, api_key)
    response = _open(url, headers, payload, timeout)
    try:
        reply = _read_openai_reply(response, entry["model"])
        return chat_to_responses_response(reply) if responses_mode else reply
    finally:
        response.close()


def probe_models(entry: Mapping[str, Any], env: Optional[Mapping[str, str]] = None,
                 timeout: float = 3.0) -> dict[str, Any]:
    """``GET {baseUrl}/models`` — no completion is requested, so nothing is billed."""
    import time

    started = time.monotonic()
    result: dict[str, Any] = {"ok": False, "http": None, "reason": None, "models": None,
                              "modelListed": None, "latencyMs": None}
    try:
        api_key = resolve_api_key(entry, env)
        anthropic = entry.get("apiMode") == "anthropic_messages"
        base = entry["baseUrl"]
        if anthropic:
            url = _url(base, "/models" if base.endswith("/v1") else "/v1/models")
            headers = {"anthropic-version": ANTHROPIC_VERSION}
            if api_key:
                headers["x-api-key"] = api_key
        else:
            url = _url(base, "/models")
            headers = {"Accept": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
        response = _open(url, headers, None, timeout, method="GET")
        try:
            data = _read_json(response)
        finally:
            response.close()
        result["http"] = 200
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ExecutionError("invalid_response", "models reply has no data list")
        ids = [str(item.get("id")) for item in items if isinstance(item, dict) and item.get("id")]
        result.update(ok=True, models=len(ids), modelListed=entry.get("model") in ids)
    except ExecutionError as exc:
        result["reason"] = exc.reason
        if exc.reason.startswith("http_"):
            result["http"] = int(exc.reason[5:])
    finally:
        result["latencyMs"] = round((time.monotonic() - started) * 1000.0, 1)
    return result


def run_probe(entry: Mapping[str, Any], prompt: str, env: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    """One explicit, cheap completion (``max_tokens`` 8) for ``/cortiq-router test``."""
    import time

    started = time.monotonic()
    request = {"messages": [{"role": "user", "content": prompt}], "max_tokens": 8}
    try:
        reply = call_endpoint(entry, request, env)
        text = reply.choices[0].message.content or ""
        return {"ok": True, "reason": None, "reply": text.strip()[:80],
                "latencyMs": round((time.monotonic() - started) * 1000.0, 1)}
    except ExecutionError as exc:
        return {"ok": False, "reason": exc.reason, "reply": None,
                "latencyMs": round((time.monotonic() - started) * 1000.0, 1)}
