"""Unit tests for dstui.events.map_notification, driven by real captured runtime traces."""

from __future__ import annotations

import copy
import dataclasses
import typing
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from deepseek_harness import Notification

from dstui.events import (
    AssistantText,
    Reasoning,
    Retrying,
    StatusChanged,
    StepStarted,
    TitleChanged,
    ToolCallStarted,
    ToolFinished,
    TurnFinished,
    UiEvent,
    UsageReported,
    map_notification,
)
from tests.conftest import TRACES_DIR, load_trace

TRACE_NAMES = sorted(path.name for path in Path(TRACES_DIR).glob("*.jsonl"))

IGNORED_EVENT_TYPES = (
    "agent/inbox/spliced",
    "approval/asked",
    "approval/decided",
    "approval/policy",
    "assistant/attempt",
    "llm/retry-started",
    "permission/preset",
    "request/context",
    "request/header",
    "sandbox/mode",
    "session-log-deepseek/delivery-accepted",
    "step/end",
    "subagent/catalog",
    "subagent/descriptor",
    "system/message",
    "turn/start",
    "user/message",
)

SUBAGENT_METHODS = ("subagent.started", "subagent.finished")
TRANSPORT_FAILURE = "DeepSeek API request to http://127.0.0.1:9/v1 failed"

UI_EVENT_TYPES = typing.get_args(UiEvent.__value__)


def _assert_well_typed(event: object) -> None:
    """A UI event of a known type whose fields match their annotations exactly."""
    assert isinstance(event, UI_EVENT_TYPES), event
    hints = typing.get_type_hints(type(event))
    for field in dataclasses.fields(event):
        value = getattr(event, field.name)
        assert isinstance(value, hints[field.name]), (event, field.name)
        assert not (hints[field.name] is int and isinstance(value, bool)), (event, field.name)


def _root_id(trace: list[Notification]) -> str:
    """Every captured trace starts with a notification of the root session."""
    return trace[0].payload["sessionId"]


def _statuses(name: str) -> list[Notification]:
    trace = load_trace(name)
    root = _root_id(trace)
    return [n for n in trace if n.method == "session.status" and n.payload["sessionId"] == root]


def _first_event_of_type(event_type: str) -> Notification:
    """The first real captured ``session.event`` of this type, from any trace."""
    for name in TRACE_NAMES:
        for notification in load_trace(name):
            event = notification.payload.get("event") or {}
            if notification.method == "session.event" and event.get("type") == event_type:
                return notification
    raise AssertionError(f"no captured {event_type!r} event")


def _root_events(name: str, event_type: str) -> list[Notification]:
    """The root session's ``session.event`` notifications of one type, in trace order."""
    trace = load_trace(name)
    root = _root_id(trace)
    return [
        n
        for n in trace
        if n.method == "session.event"
        and n.payload["sessionId"] == root
        and n.payload["event"]["type"] == event_type
    ]


def _mapped(name: str, event_type: str) -> list[list[UiEvent]]:
    """map_notification applied to each root event of one type in a trace."""
    root = _root_id(load_trace(name))
    return [map_notification(n, root) for n in _root_events(name, event_type)]


def _event(session_id: str, event_type: object, data: object) -> Notification:
    return Notification(
        "session.event", {"sessionId": session_id, "event": {"type": event_type, "data": data}}
    )


def test_session_status_maps_to_status_changed() -> None:
    trace = load_trace("text.jsonl")
    running, idle = _statuses("text.jsonl")[:2]

    assert map_notification(running, _root_id(trace)) == [StatusChanged("running")]
    assert map_notification(idle, _root_id(trace)) == [StatusChanged("idle")]


@pytest.mark.parametrize("event_type", IGNORED_EVENT_TYPES)
def test_ignored_event_types_map_to_nothing(event_type: str) -> None:
    notification = _first_event_of_type(event_type)

    assert map_notification(notification, notification.payload["sessionId"]) == []


def test_unknown_event_type_maps_to_nothing() -> None:
    notification = _event("s-1", "future/thing", {"status": "running", "title": "x"})

    assert map_notification(notification, "s-1") == []


@pytest.mark.parametrize("method", ["session.unknown", "session.statuses", "", "subagent.started"])
def test_unknown_methods_of_root_session_map_to_nothing(method: str) -> None:
    notification = Notification(method, {"sessionId": "s-1", "status": "running"})

    assert map_notification(notification, "s-1") == []


def test_subagent_child_session_notifications_do_not_leak() -> None:
    trace = load_trace("sdk-subagent.jsonl")
    root = _root_id(trace)
    foreign = [n for n in trace if n.payload.get("sessionId") != root]
    child_events = [n for n in foreign if n.method == "session.event"]

    assert {n.method for n in foreign} == {"session.event", "session.status", *SUBAGENT_METHODS}
    assert {n.payload["event"]["type"] for n in child_events} >= {
        "assistant/message",
        "session/title",
        "step/start",
        "turn/end",
    }
    assert [event for n in foreign for event in map_notification(n, root)] == []


@pytest.mark.parametrize(
    ("name", "title"),
    [
        ("text.jsonl", "hello"),
        ("reasoning.jsonl", "what is the answer?"),
    ],
)
def test_session_title_maps_to_title_changed(name: str, title: str) -> None:
    assert _mapped(name, "session/title") == [[TitleChanged(title)]]


def test_step_start_maps_to_step_started() -> None:
    assert _mapped("tool-minimal.jsonl", "step/start") == [
        [StepStarted(turn=1, step=1)],
        [StepStarted(turn=1, step=2)],
    ]
    assert _mapped("text.jsonl", "step/start") == [
        [StepStarted(turn=1, step=1)],
        [StepStarted(turn=2, step=1)],
    ]


def test_assistant_text_maps_to_text_then_usage() -> None:
    assert _mapped("text.jsonl", "assistant/message") == [
        [AssistantText("Hello! I am a fake DeepSeek model."), UsageReported(11, 7, 0)],
        [AssistantText("You said: hello."), UsageReported(11, 7, 0)],
    ]


def test_truncated_reply_text_and_cache_read_usage() -> None:
    assert _mapped("max-tokens.jsonl", "assistant/message") == [
        [
            AssistantText("truncated answ"),
            UsageReported(input_tokens=56, output_tokens=30, cache_read_tokens=64),
        ]
    ]


def test_reasoning_comes_before_text_and_usage_last() -> None:
    assert _mapped("reasoning.jsonl", "assistant/message") == [
        [
            Reasoning("The user asks a question; I will answer 42."),
            AssistantText("The answer is 42."),
            UsageReported(11, 7, 0),
        ],
        [AssistantText("Still 42."), UsageReported(11, 7, 0)],
    ]


def test_tool_call_blocks_are_skipped_in_assistant_message() -> None:
    assert _mapped("tool-minimal.jsonl", "assistant/message") == [
        [
            Reasoning("Need to run echo."),
            AssistantText("Let me run that."),
            UsageReported(11, 7, 0),
        ],
        [AssistantText("The command printed: hi"), UsageReported(11, 7, 0)],
    ]
    assert _mapped("sdk-tool.jsonl", "assistant/message") == [
        [
            Reasoning("I should call bash (leg 0)."),
            AssistantText("Calling bash."),
            UsageReported(56, 30, 64),
        ],
        [AssistantText('Tool said: "tool-ok\\n"'), UsageReported(56, 30, 64)],
    ]


def test_assistant_blocks_are_emitted_in_content_order_with_usage_last() -> None:
    content = [
        {"type": "reasoning", "text": "r1"},
        {"type": "text", "text": "t1"},
        {"type": "tool-call", "id": "c1", "name": "bash", "arguments": "{}"},
        {"type": "reasoning", "text": "r2"},
        {"type": "text", "text": "t2"},
    ]
    usage = {"inputTokens": 3, "outputTokens": 2, "totalTokens": 5, "cacheReadTokens": 1}
    data = {"message": {"role": "assistant", "content": content}, "usage": usage}

    assert map_notification(_event("s", "assistant/message", data), "s") == [
        Reasoning("r1"),
        AssistantText("t1"),
        Reasoning("r2"),
        AssistantText("t2"),
        UsageReported(input_tokens=3, output_tokens=2, cache_read_tokens=1),
    ]


@pytest.mark.parametrize("usage", [None, {}, [], "11"])
def test_absent_or_empty_usage_is_not_reported(usage: object) -> None:
    data = {"message": {"content": [{"type": "text", "text": "hi"}]}, "usage": usage}

    assert map_notification(_event("s", "assistant/message", data), "s") == [AssistantText("hi")]


def test_tool_call_maps_to_tool_call_started() -> None:
    assert _mapped("tool-minimal.jsonl", "tool/call") == [
        [ToolCallStarted("call_echo_1", "bash", '{"command": "echo hi"}')]
    ]


def test_tool_result_v015_shape_maps_to_tool_finished() -> None:
    assert _mapped("tool-minimal.jsonl", "tool/result") == [
        [ToolFinished("call_echo_1", "hi\n[Command finished with exit code 0]", is_error=False)]
    ]


def test_tool_result_v015_shape_reports_errors() -> None:
    denied, escalation = _mapped("sdk-escalate.jsonl", "tool/result")

    assert denied == [
        ToolFinished(
            "call_escalate_0",
            "[stderr]\ntouch: cannot touch '/usr/dsh-mock-denied': Read-only file system\n"
            "[sandbox: file access denied under workspace-write mode]\n"
            "[sandbox: escalation available \u2014 retry this exact command once with "
            "sandbox_permissions (the narrowest wider mode that suffices) + justification; "
            "the approval prompt asks the user]\n[exit code: 1]",
            is_error=False,
        )
    ]
    assert escalation == [
        ToolFinished(
            "call_escalate_1",
            'Error: sandbox escalation to "danger-full-access" requires approval, '
            "but no approval channel is available",
            is_error=True,
        )
    ]


def test_tool_result_v017_shape_maps_to_tool_finished() -> None:
    assert _mapped("v017-sdk.jsonl", "tool/result") == [
        [ToolFinished("call_fake_1", "hi\n", is_error=False)]
    ]


def test_tool_result_v017_shape_reports_errors_and_joins_only_text_parts() -> None:
    parts = [
        {"type": "text", "text": "Error: boom"},
        {"type": "image", "data": "..."},
        {"type": "text", "text": "details"},
    ]
    message = {"role": "tool", "toolCallId": "c9", "isError": True, "content": parts}

    assert map_notification(_event("s", "tool/result", {"message": message}), "s") == [
        ToolFinished("c9", "Error: boom\ndetails", is_error=True)
    ]


def test_llm_retry_maps_to_retrying() -> None:
    delays = [545.1917724303804, 927.9233614435861, 1870.7053179483405, 3662.8842453978673]
    delays.append(8125.579772355584)

    assert _mapped("retry-500.jsonl", "llm/retry") == [
        [Retrying(attempt, 5, delay, "SERVER", "mock server exploded")]
        for attempt, delay in enumerate(delays, start=1)
    ]


def test_transport_retry_without_status() -> None:
    first = _mapped("transport-retry.jsonl", "llm/retry")[0]

    assert first == [Retrying(1, 5, 533.0272403434219, "TRANSPORT", TRANSPORT_FAILURE)]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("text.jsonl", [TurnFinished("completed"), TurnFinished("completed")]),
        ("max-tokens.jsonl", [TurnFinished("max-tokens")]),
        (
            "error-401.jsonl",
            [
                TurnFinished(
                    "error", "AUTH", "Authentication Fails, Your api key: ****mock is invalid"
                )
            ],
        ),
        ("error-402.jsonl", [TurnFinished("error", "QUOTA", "Insufficient Balance")]),
        ("retry-500.jsonl", [TurnFinished("error", "SERVER", "mock server exploded")]),
        ("transport-retry.jsonl", [TurnFinished("error", "TRANSPORT", TRANSPORT_FAILURE)]),
    ],
)
def test_turn_end_maps_to_turn_finished(name: str, expected: list[TurnFinished]) -> None:
    assert _mapped(name, "turn/end") == [[finished] for finished in expected]


def _replay(name: str) -> list[UiEvent]:
    """Every UI event of a whole captured trace, mapped for its root session."""
    trace = load_trace(name)
    root = _root_id(trace)
    return [event for notification in trace for event in map_notification(notification, root)]


def test_replay_subagent_trace_shows_only_the_root_session() -> None:
    child_output = "Hello from mock (scenario=hello, user_prompts_seen=1, msgs=4)."

    assert _replay("sdk-subagent.jsonl") == [
        StatusChanged("running"),
        StepStarted(1, 1),
        TitleChanged("MOCK:subagent"),
        Reasoning("I should call subagent (leg 0)."),
        AssistantText("Calling subagent."),
        UsageReported(56, 30, 64),
        ToolCallStarted(
            "call_subagent_0",
            "subagent",
            '{"description": "Say hello", "prompt": "MOCK:hello from child", '
            '"run_in_background": false}',
        ),
        ToolFinished("call_subagent_0", child_output, is_error=False),
        StepStarted(1, 2),
        AssistantText(f'Tool said: "{child_output}"'),
        UsageReported(56, 30, 64),
        TurnFinished("completed"),
        StatusChanged("idle"),
    ]


def test_replay_v017_trace() -> None:
    assert _replay("v017-sdk.jsonl") == [
        StatusChanged("running"),
        StepStarted(1, 1),
        TitleChanged("hi"),
        Reasoning("thinking"),
        AssistantText("Hello from HEAD"),
        UsageReported(11, 7, 0),
        TurnFinished("completed"),
        StatusChanged("idle"),
        StatusChanged("running"),
        StepStarted(2, 1),
        UsageReported(11, 7, 0),
        ToolCallStarted("call_fake_1", "bash", '{"command": "echo hi", "description": "echo"}'),
        ToolFinished("call_fake_1", "hi\n", is_error=False),
        StepStarted(2, 2),
        AssistantText("tool done"),
        UsageReported(11, 7, 0),
        TurnFinished("completed"),
        StatusChanged("idle"),
        StatusChanged("running"),
        StepStarted(3, 1),
        TurnFinished("error", "AUTH", "Authentication Fails, Your api key: ****fake is invalid"),
        StatusChanged("idle"),
    ]


def _session_ids(trace: list[Notification]) -> set[str]:
    ids = {n.payload.get("sessionId") for n in trace} | {
        n.payload.get("childSessionId") for n in trace
    }
    return {session_id for session_id in ids if isinstance(session_id, str)}


@pytest.mark.parametrize("name", TRACE_NAMES)
def test_every_captured_notification_maps_to_well_typed_events(name: str) -> None:
    trace = load_trace(name)
    candidates = _session_ids(trace) | {"", "no-such-session"}
    mapped = 0

    for notification in trace:
        for root in candidates:
            events = map_notification(notification, root)
            assert isinstance(events, list)
            for event in events:
                _assert_well_typed(event)
            mapped += len(events)

    assert mapped > 0


# -- totality: missing or wrong-typed fields inside the payload never raise ---------------
# (the SDK itself guarantees ``method: str`` and ``payload: dict``: client._handle_message)


def _tool_result(message: object) -> Notification:
    return _event("s", "tool/result", {"message": message})


# Defaults that matter; the fuzz below proves every other malformation maps without raising.
MALFORMED: list[tuple[str, Notification, list[UiEvent]]] = [
    ("non-int numbers", _event("s", "step/start", {"turn": "1", "step": 2.0}), [
        StepStarted(0, 0)
    ]),
    ("usage non-int numbers", _event("s", "assistant/message", {"usage": {
        "inputTokens": "11", "outputTokens": None, "cacheReadTokens": 2.9
    }}), [UsageReported(0, 0, 0)]),
    ("retry int delay", _event("s", "llm/retry", {"delayMs": 250}), [
        Retrying(0, 0, 250.0, "", "")
    ]),
    ("turn end wrong-typed error", _event("s", "turn/end", {"reason": {
        "kind": "error", "error": {"code": 401, "message": ["x"]}
    }}), [TurnFinished("error", code=None, message=None)]),
    ("bare string content", _event("s", "assistant/message", {"message": {"content": "x"}}), [
        AssistantText("x")
    ]),
    ("empty, unknown or junk blocks", _event("s", "assistant/message", {"message": {"content": [
        {"type": "reasoning", "text": ""}, {"type": "text", "text": "\n\n"},
        {"type": "tool-call", "id": "c1", "name": "bash", "arguments": "{}"},
        {"type": "image", "text": "alt"}, {"type": ["text"], "text": "x"}, None, 5,
    ]}}), []),
    ("isError is not true", _tool_result({"content": [
        {"type": "tool-result", "toolCallId": "c1", "isError": "1", "content": None}
    ]}), [ToolFinished("c1", "", is_error=False)]),
    ("v017 bare string", _tool_result({"role": "tool", "toolCallId": "c1", "content": "out"}), [
        ToolFinished("c1", "out", is_error=False)
    ]),
]  # fmt: skip


@pytest.mark.parametrize(
    ("notification", "expected"), [pytest.param(n, e, id=case) for case, n, e in MALFORMED]
)
def test_malformed_fields_map_to_defaults(
    notification: Notification, expected: list[UiEvent]
) -> None:
    events = map_notification(notification, "s")

    assert events == expected
    for event in events:
        _assert_well_typed(event)  # e.g. 0, not False, where an int is expected


type JsonPath = tuple[str | int, ...]

WRONG_VALUES: tuple[object, ...] = (
    None,
    True,
    2.5,
    "x",
    [None, 7, {"type": "text", "text": 1}],
    {"type": "tool-result", "text": None},
)

FUZZ_SAMPLES = (
    ("text.jsonl", None),  # session.status
    ("tool-minimal.jsonl", "assistant/message"),
    ("tool-minimal.jsonl", "tool/call"),
    ("tool-minimal.jsonl", "tool/result"),
    ("v017-sdk.jsonl", "tool/result"),
    ("retry-500.jsonl", "llm/retry"),
    ("error-401.jsonl", "turn/end"),
    ("text.jsonl", "session/title"),
    ("text.jsonl", "step/start"),
)


def _paths(value: object, prefix: JsonPath = ()) -> Iterator[JsonPath]:
    """Every key/index path inside a JSON value (not the value itself)."""
    children: list[tuple[str | int, object]] = []
    if isinstance(value, dict):
        children = list(value.items())
    elif isinstance(value, list):
        children = list(enumerate(value))
    for key, child in children:
        yield (*prefix, key)
        yield from _paths(child, (*prefix, key))


def _with(payload: object, path: JsonPath, value: object, *, delete: bool = False) -> Any:
    """A deep copy of payload with the value at path replaced (or deleted)."""
    result = copy.deepcopy(payload)
    parent: Any = result
    for key in path[:-1]:
        parent = parent[key]
    if delete:
        del parent[path[-1]]
    else:
        parent[path[-1]] = value
    return result


def _variants(sample: Notification) -> Iterator[tuple[str, Notification]]:
    """The sample with each payload field, at every depth, deleted or set to a wrong value."""
    for path in _paths(sample.payload):
        yield (
            f"del {path}",
            Notification(sample.method, _with(sample.payload, path, None, delete=True)),
        )
        for value in WRONG_VALUES:
            yield (
                f"{path}={value!r}",
                Notification(sample.method, _with(sample.payload, path, value)),
            )


@pytest.mark.parametrize(("name", "event_type"), FUZZ_SAMPLES)
def test_missing_or_wrong_typed_fields_at_any_depth_never_raise(
    name: str, event_type: str | None
) -> None:
    sample = _statuses(name)[0] if event_type is None else _root_events(name, event_type)[0]
    failures: list[str] = []

    for label, variant in _variants(sample):
        try:
            for event in map_notification(variant, sample.payload["sessionId"]):
                _assert_well_typed(event)
        except Exception as exc:  # collect every failure for one readable report
            failures.append(f"{label}: {exc!r}")

    assert failures == []
