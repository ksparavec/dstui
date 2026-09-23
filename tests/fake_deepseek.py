"""Stdlib-only fake DeepSeek endpoint for keyless end-to-end tests of the real runtime.

Point the real SDK at it with ``DeepSeekHarness(api_key="sk-fake", base_url=fake.url, ...)``
(the SDK exports both as DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL to the runtime).

Two wire dialects are served, selected by request path, because the runtime changed dialect
between releases:

* ``POST {base}/chat/completions`` -- OpenAI-compatible Chat Completions streaming.
  Used by the PyPI wheel ``deepseek-harness-runtime-bin 0.1.5rc1`` (verified empirically).
  SSE frames are ``data: <json>\\n\\n`` and the stream MUST end with ``data: [DONE]``;
  the runtime raises STREAM_CLOSED otherwise.
* ``POST {base}/v1/messages`` -- Anthropic Messages streaming (``event: <type>`` +
  ``data: <json>``; ``message_stop`` terminates, no ``[DONE]``). Used by the GitHub HEAD
  runtime (dsh-v0.1.7-alpha.2, packages/llm/llm-deepseek/src/adapter.ts:199).

Replies are scripted dialect-neutrally (text / reasoning / tool calls / HTTP error) and
rendered for whichever dialect the runtime asks for.

Usage::

    with FakeDeepSeek() as fake:
        fake.enqueue(text_reply("Hello there!", reasoning="user greets me"))
        fake.enqueue(tool_call_reply("bash", {"command": "echo hi"}), text_reply("done"))
        ...
        fake.requests  # decoded JSON bodies of every model request, arrival order
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

JsonObject = dict[str, Any]

OPENAI_PATHS = frozenset({"/chat/completions", "/v1/chat/completions"})
MESSAGES_PATHS = frozenset({"/v1/messages", "/messages"})
MODEL_PATHS = OPENAI_PATHS | MESSAGES_PATHS


# ----------------------------------------------------------------------------- scripted replies


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    arguments: JsonObject
    call_id: str = "call_fake_1"


@dataclass(frozen=True, slots=True)
class Reply:
    """One scripted answer to one model request (dialect-neutral)."""

    text: str | None = None
    reasoning: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    finish: str | None = None  # "stop" | "tool_calls" | "length"; derived when None
    chunks: int = 3  # how many SSE deltas the text/reasoning/arguments are split into
    chunk_delay_s: float = 0.0
    status: int = 200
    error: JsonObject | None = None  # {"type", "message", "code"?}
    headers: tuple[tuple[str, str], ...] = ()
    prompt_tokens: int = 11
    completion_tokens: int = 7

    @property
    def finish_reason(self) -> str:
        if self.finish is not None:
            return self.finish
        return "tool_calls" if self.tool_calls else "stop"

    @property
    def kind(self) -> str:
        return f"http_{self.status}" if self.status != 200 else f"sse_{self.finish_reason}"


def text_reply(
    text: str,
    *,
    reasoning: str | None = None,
    chunks: int = 3,
    chunk_delay_s: float = 0.0,
    finish: str | None = None,
) -> Reply:
    return Reply(
        text=text, reasoning=reasoning, chunks=chunks, chunk_delay_s=chunk_delay_s, finish=finish
    )


def tool_call_reply(
    name: str,
    arguments: JsonObject,
    *,
    call_id: str = "call_fake_1",
    preface: str | None = None,
    reasoning: str | None = None,
) -> Reply:
    return Reply(
        text=preface, reasoning=reasoning, tool_calls=(ToolCall(name, arguments, call_id),)
    )


def error_reply(
    status: int,
    message: str = "fake provider error",
    *,
    error_type: str = "api_error",
    code: str | None = None,
    retry_after: str | None = None,
) -> Reply:
    error: JsonObject = {"type": error_type, "message": message}
    if code is not None:
        error["code"] = code
    headers = (("retry-after", retry_after),) if retry_after is not None else ()
    return Reply(status=status, error=error, headers=headers)


def auth_error_reply() -> Reply:
    return error_reply(
        401,
        "Authentication Fails, Your api key: ****fake is invalid",
        error_type="authentication_error",
        code="invalid_request_error",
    )


def _split(text: str, parts: int) -> list[str]:
    if parts <= 1 or len(text) <= 1:
        return [text]
    size = max(1, -(-len(text) // parts))
    return [text[i : i + size] for i in range(0, len(text), size)]


# ----------------------------------------------------------------------------- dialect renderers


def render_openai(reply: Reply, model: str) -> list[str]:
    """DeepSeek /chat/completions stream: data-only frames, reasoning_content, [DONE]."""
    created = int(time.time())

    def chunk(delta: JsonObject, finish: str | None = None, usage: JsonObject | None = None) -> str:
        payload: JsonObject = {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "system_fingerprint": "fp_fake",
            "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}],
        }
        if usage is not None:
            payload["usage"] = usage
        return json.dumps(payload)

    frames = [chunk({"role": "assistant", "content": ""})]
    if reply.reasoning:
        frames += [
            chunk({"content": None, "reasoning_content": p})
            for p in _split(reply.reasoning, reply.chunks)
        ]
    if reply.text:
        frames += [chunk({"content": p}) for p in _split(reply.text, reply.chunks)]
    for index, call in enumerate(reply.tool_calls):
        raw = json.dumps(call.arguments)
        parts = _split(raw, reply.chunks)
        frames.append(
            chunk(
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call.call_id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": parts[0]},
                        }
                    ]
                }
            )
        )
        frames += [
            chunk({"tool_calls": [{"index": index, "function": {"arguments": p}}]})
            for p in parts[1:]
        ]
    usage = {
        "prompt_tokens": reply.prompt_tokens,
        "completion_tokens": reply.completion_tokens,
        "total_tokens": reply.prompt_tokens + reply.completion_tokens,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": reply.prompt_tokens,
    }
    frames.append(chunk({"content": ""}, reply.finish_reason, usage))
    frames.append("[DONE]")
    return [f"data: {frame}\n\n" for frame in frames]


def render_messages(reply: Reply, model: str) -> list[str]:
    """Anthropic Messages stream (runtime >= 0.1.7): event+data frames, message_stop terminates."""
    stop = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}[
        reply.finish_reason
    ]
    events: list[JsonObject] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_fake",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "usage": {"input_tokens": reply.prompt_tokens, "output_tokens": 0},
            },
        }
    ]
    index = 0
    if reply.reasoning:
        events.append(
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "thinking", "thinking": ""},
            }
        )
        events += [
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "thinking_delta", "thinking": p},
            }
            for p in _split(reply.reasoning, reply.chunks)
        ]
        events.append({"type": "content_block_stop", "index": index})
        index += 1
    if reply.text:
        events.append(
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "text", "text": ""},
            }
        )
        events += [
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "text_delta", "text": p},
            }
            for p in _split(reply.text, reply.chunks)
        ]
        events.append({"type": "content_block_stop", "index": index})
        index += 1
    for call in reply.tool_calls:
        events.append(
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {
                    "type": "tool_use",
                    "id": call.call_id,
                    "name": call.name,
                    "input": {},
                },
            }
        )
        events += [
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "input_json_delta", "partial_json": p},
            }
            for p in _split(json.dumps(call.arguments), reply.chunks)
        ]
        events.append({"type": "content_block_stop", "index": index})
        index += 1
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop, "stop_sequence": None},
            "usage": {"output_tokens": reply.completion_tokens},
        }
    )
    events.append({"type": "message_stop"})
    return [f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events]


# ----------------------------------------------------------------------------- server


@dataclass(slots=True)
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: JsonObject | None
    reply_kind: str
    at: float


@dataclass(slots=True)
class _State:
    lock: threading.Lock = field(default_factory=threading.Lock)
    script: deque[Reply] = field(default_factory=deque)
    requests: list[RecordedRequest] = field(default_factory=list)
    responder: Callable[[JsonObject], Reply] | None = None
    default: Reply | None = None
    api_key: str | None = None


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeDeepSeek/1.0"
    protocol_version = "HTTP/1.1"
    state: _State  # bound per server instance

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _read_body(self) -> JsonObject | None:
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw.decode("utf-8", "replace")}
        return value if isinstance(value, dict) else {"_raw": value}

    def _record(self, body: JsonObject | None, kind: str) -> None:
        headers = {k.lower(): v for k, v in self.headers.items()}
        with self.state.lock:
            self.state.requests.append(
                RecordedRequest(self.command, self.path, headers, body, kind, time.monotonic())
            )

    def _send_json(
        self, status: int, payload: JsonObject, extra: tuple[tuple[str, str], ...] = ()
    ) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._record(None, "unexpected_get")
        self._send_json(404, {"error": {"type": "not_found_error", "message": f"GET {self.path}"}})

    def do_POST(self) -> None:
        body = self._read_body()
        path = self.path.split("?", 1)[0].rstrip("/")
        if path not in MODEL_PATHS:
            self._record(body, "unexpected_path")
            self._send_json(404, {"error": {"type": "not_found_error", "message": f"POST {path}"}})
            return
        if self.state.api_key is not None and self._presented_key() != self.state.api_key:
            self._record(body, "bad_key")
            self._send_json(401, {"error": {"type": "authentication_error", "message": "bad key"}})
            return
        reply = self._next_reply(body or {})
        self._record(body, reply.kind)
        if reply.status != 200:
            self._send_json(reply.status, {"error": reply.error or {}}, reply.headers)
            return
        model = str((body or {}).get("model", "fake-model"))
        frames = (
            render_openai(reply, model) if path in OPENAI_PATHS else render_messages(reply, model)
        )
        self._stream(frames, reply.chunk_delay_s)

    def _presented_key(self) -> str | None:
        auth = self.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:]
        return self.headers.get("x-api-key")

    def _next_reply(self, body: JsonObject) -> Reply:
        with self.state.lock:
            if self.state.script:
                return self.state.script.popleft()
            responder, default = self.state.responder, self.state.default
        if responder is not None:
            return responder(body)
        if default is not None:
            return default
        return error_reply(500, "fake script exhausted", code="SCRIPT_EXHAUSTED")

    def _stream(self, frames: list[str], delay_s: float) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        self.close_connection = True
        for frame in frames:
            try:
                self.wfile.write(frame.encode())
                self.wfile.flush()
            except BrokenPipeError, ConnectionResetError:
                return
            if delay_s:
                time.sleep(delay_s)


class FakeDeepSeek:
    """In-process fake DeepSeek endpoint on 127.0.0.1:<ephemeral>, served from a daemon thread."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        api_key: str | None = None,
        default: Reply | None = None,
        responder: Callable[[JsonObject], Reply] | None = None,
    ) -> None:
        self._state = _State(api_key=api_key, default=default, responder=responder)
        handler = type("_BoundHandler", (_Handler,), {"state": self._state})
        self._server = ThreadingHTTPServer((host, port), handler)
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    def start(self) -> FakeDeepSeek:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._server.serve_forever, name="fake-deepseek", daemon=True
            )
            self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5)
            self._thread = None
        self._server.server_close()

    def __enter__(self) -> FakeDeepSeek:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    @property
    def url(self) -> str:
        """Value for base_url / DEEPSEEK_BASE_URL (no trailing path)."""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def enqueue(self, *replies: Reply) -> None:
        with self._state.lock:
            self._state.script.extend(replies)

    def set_default(self, reply: Reply | None) -> None:
        with self._state.lock:
            self._state.default = reply

    def set_responder(self, responder: Callable[[JsonObject], Reply] | None) -> None:
        with self._state.lock:
            self._state.responder = responder

    def reset(self) -> None:
        with self._state.lock:
            self._state.script.clear()
            self._state.requests.clear()

    @property
    def pending(self) -> int:
        with self._state.lock:
            return len(self._state.script)

    @property
    def recorded(self) -> list[RecordedRequest]:
        with self._state.lock:
            return list(self._state.requests)

    @property
    def requests(self) -> list[JsonObject]:
        """Decoded JSON bodies of every model request (either dialect), arrival order."""
        return [
            r.body or {} for r in self.recorded if r.path.split("?")[0].rstrip("/") in MODEL_PATHS
        ]


# ----------------------------------------------------------------------------- assertion helpers


def message_text(message: JsonObject) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        b.get("text", "") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)
    )


def conversation(body: JsonObject) -> list[tuple[str, str]]:
    """``[(role, text)]`` for one request body; OpenAI ``tool`` messages keep role ``tool``."""
    out = [
        (str(m.get("role", "?")), message_text(m))
        for m in body.get("messages", [])
        if isinstance(m, dict)
    ]
    if isinstance(body.get("system"), str):
        out.insert(0, ("system", body["system"]))
    return out


def tool_names(body: JsonObject) -> list[str]:
    names = []
    for tool in body.get("tools", []) or []:
        if isinstance(tool, dict):
            names.append(tool.get("function", {}).get("name") or tool.get("name") or "?")
    return names
