"""Translate DeepSeek Harness SDK notifications into UI events.

Pure functions only: no Textual, no threads. The runtime never streams tokens;
every ``session.event`` arrives as one committed record, so each UI event maps
to a whole chunk of the conversation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from deepseek_harness import Notification


@dataclass(frozen=True, slots=True)
class Reasoning:
    """The model's reasoning ("thinking") text for one step."""

    text: str


@dataclass(frozen=True, slots=True)
class AssistantText:
    """Visible assistant reply text (Markdown) for one step."""

    text: str


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    call_id: str
    name: str
    arguments: str  # raw JSON string as sent by the model


@dataclass(frozen=True, slots=True)
class ToolFinished:
    call_id: str
    output: str
    is_error: bool


@dataclass(frozen=True, slots=True)
class Retrying:
    attempt: int
    max_retries: int
    delay_ms: float
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class TurnFinished:
    kind: str  # completed | error | max-tokens | aborted | blocked | interrupted | forked
    code: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class StatusChanged:
    status: str  # running | idle


@dataclass(frozen=True, slots=True)
class TitleChanged:
    title: str


@dataclass(frozen=True, slots=True)
class UsageReported:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int


@dataclass(frozen=True, slots=True)
class StepStarted:
    turn: int
    step: int


type UiEvent = (
    Reasoning
    | AssistantText
    | ToolCallStarted
    | ToolFinished
    | Retrying
    | TurnFinished
    | StatusChanged
    | TitleChanged
    | UsageReported
    | StepStarted
)


def map_notification(notification: Notification, root_session_id: str) -> list[UiEvent]:
    """Translate one SDK notification of the root session; everything else maps to [].

    The SDK always delivers ``method: str`` and ``payload: dict``. Inside the payload,
    missing or wrong-typed fields yield [] or events with default values
    ("" / 0 / 0.0 / False / None), never an exception.
    """
    payload = notification.payload
    if payload.get("sessionId") != root_session_id:
        return []  # subagent sessions; subagent.* notifications carry no sessionId
    if notification.method == "session.status":
        return [StatusChanged(_str(payload.get("status")))]
    if notification.method != "session.event":
        return []
    event = _dict(payload.get("event"))
    mapper = _EVENT_MAPPERS.get(_str(event.get("type")))
    return [] if mapper is None else mapper(_dict(event.get("data")))


# -- total accessors: wrong-typed or missing values become defaults -------------------


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: object) -> int:
    """A JSON integer (``true`` is not one); anything else is 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _float(value: object) -> float:
    return value if isinstance(value, float) else float(_int(value))


def _blocks(content: object) -> list[dict[str, Any]]:
    """Message content as a list of block dicts; a bare string counts as one text block."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [block for block in content if isinstance(block, dict)]
    return []


# -- one mapper per session.event type --------------------------------------------------


def _assistant_message(data: dict[str, Any]) -> list[UiEvent]:
    """Reasoning/text per content block in order, then usage; tool-call blocks are skipped."""
    content = _blocks(_dict(data.get("message")).get("content"))
    texts = [event for event in map(_text_event, content) if event is not None]
    usage = data.get("usage")
    if not isinstance(usage, dict) or not usage:
        return texts
    reported = UsageReported(
        _int(usage.get("inputTokens")),
        _int(usage.get("outputTokens")),
        _int(usage.get("cacheReadTokens")),
    )
    return [*texts, reported]


def _text_event(block: dict[str, Any]) -> UiEvent | None:
    text = _str(block.get("text"))
    if not text.strip():
        return None
    kind = _str(block.get("type"))
    if kind == "reasoning":
        return Reasoning(text)
    if kind == "text":
        return AssistantText(text)
    return None  # "tool-call" blocks are rendered from the tool/call event that follows


def _tool_call(data: dict[str, Any]) -> list[UiEvent]:
    call_id, name = _str(data.get("callId")), _str(data.get("name"))
    return [ToolCallStarted(call_id, name, arguments=_str(data.get("arguments")))]


def _tool_result(data: dict[str, Any]) -> list[UiEvent]:
    message = _dict(data.get("message"))
    if message.get("role") == "tool":  # runtime 0.1.7: the message itself is the result
        return [_tool_finished(message)]
    # runtime 0.1.5: role "user" with one "tool-result" block per call
    content = _blocks(message.get("content"))
    return [_tool_finished(block) for block in content if block.get("type") == "tool-result"]


def _tool_finished(result: dict[str, Any]) -> ToolFinished:
    """Joins the result's text parts (one per line); isError counts only when it is true."""
    parts = _blocks(result.get("content"))
    texts = [_str(part.get("text")) for part in parts if part.get("type") == "text"]
    output = "\n".join(text for text in texts if text)
    return ToolFinished(_str(result.get("toolCallId")), output, result.get("isError") is True)


def _retry(data: dict[str, Any]) -> list[UiEvent]:
    failure = _dict(data.get("failure"))
    return [
        Retrying(
            attempt=_int(data.get("retry")),
            max_retries=_int(data.get("maxRetries")),
            delay_ms=_float(data.get("delayMs")),
            code=_str(failure.get("code")),
            message=_str(failure.get("message")),
        )
    ]


def _turn_end(data: dict[str, Any]) -> list[UiEvent]:
    reason = _dict(data.get("reason"))
    error = _dict(reason.get("error"))
    code, message = _optional_str(error.get("code")), _optional_str(error.get("message"))
    return [TurnFinished(_str(reason.get("kind")), code, message)]


def _title(data: dict[str, Any]) -> list[UiEvent]:
    return [TitleChanged(_str(data.get("title")))]


def _step_start(data: dict[str, Any]) -> list[UiEvent]:
    return [StepStarted(_int(data.get("turn")), _int(data.get("step")))]


_EVENT_MAPPERS: dict[str, Callable[[dict[str, Any]], list[UiEvent]]] = {
    "assistant/message": _assistant_message,
    "tool/call": _tool_call,
    "tool/result": _tool_result,
    "llm/retry": _retry,
    "turn/end": _turn_end,
    "session/title": _title,
    "step/start": _step_start,
}
