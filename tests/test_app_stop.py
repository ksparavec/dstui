"""DsTuiApp Stop (Escape) and New conversation (Ctrl+N), against ``FakeBackend``."""

from __future__ import annotations

import threading
from pathlib import Path

from deepseek_harness.errors import TransportClosedError
from textual.command import CommandPalette
from textual.widgets._collapsible import CollapsibleTitle

from dstui.events import (
    AssistantText,
    Reasoning,
    Retrying,
    StepStarted,
    TitleChanged,
    ToolCallStarted,
    ToolFinished,
    UsageReported,
)
from dstui.widgets import (
    AssistantMessage,
    Notice,
    PromptArea,
    ToolCallBlock,
)
from tests.fake_backend import FakeBackend
from tests.helpers_app import (
    INTERRUPTED,
    MODEL,
    PROFILE,
    assert_off_loop,
    make_app,
    ready_app,
    submit,
)
from tests.helpers_ui import (
    SIZE,
    STOPPED,
    body_text,
    chat_items,
    notices,
    status,
    wait_until,
)


async def test_escape_stops_the_running_turn(tmp_path: Path) -> None:
    cancel_gate = threading.Event()  # holds cancel() like a slow runtime shutdown
    backend = FakeBackend(gates={"cancel": cancel_gate})
    backend.script(TitleChanged("Long task"), AssistantText("partial"), threading.Event())
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "long task")
        await wait_until(pilot, backend.blocked.is_set)
        assert app.sub_title == "Long task"
        await pilot.press("escape")
        await wait_until(pilot, lambda: status(app).startswith("stopping… │"))
        await pilot.press("escape")  # a second Escape while stopping is ignored
        assert app.busy
        cancel_gate.set()
        await wait_until(pilot, lambda: not app.busy)
        assert notices(app, "info") == [STOPPED]
        assert app.sub_title == "new conversation"
        assert status(app).startswith("ready │")
        assert [m.source for m in chat_items(app, AssistantMessage)] == ["partial"]
        assert app.focused is app.query_one("#prompt", PromptArea)
    assert len(backend.calls_named("cancel")) == 1
    assert_off_loop(backend, "cancel")


async def test_stopping_mid_tool_marks_the_tool_interrupted(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.script(ToolCallStarted("c1", "bash", '{"command": "sleep 30"}'), threading.Event())
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "go")
        await wait_until(pilot, backend.blocked.is_set)
        await pilot.press("escape")
        await wait_until(pilot, lambda: not app.busy)
        (block,) = chat_items(app, ToolCallBlock)
        assert (block.output, block.is_error) == (INTERRUPTED, True)
        assert str(block.title) == "tool: bash (error)"
        assert "running…" not in body_text(block)
        assert notices(app, "info") == [STOPPED]


async def test_a_crash_mid_tool_marks_only_the_unfinished_tool_interrupted(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.script(
        ToolCallStarted("c1", "bash", '{"command": "ls"}'),
        ToolCallStarted("c2", "bash", '{"command": "sleep 30"}'),
        ToolFinished("c1", "a.txt", False),
        error=TransportClosedError("runtime stdout closed"),
    )
    backend.script(ToolFinished("c2", "a stray late result", False))  # ids belong to a turn
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "go")
        await wait_until(pilot, lambda: len(backend.sent) == 1 and not app.busy)
        await submit(pilot, app, "again")
        await wait_until(pilot, lambda: len(backend.sent) == 2 and not app.busy)
        finished, interrupted = chat_items(app, ToolCallBlock)
        assert (finished.output, finished.is_error) == ("a.txt", False)
        assert (interrupted.output, interrupted.is_error) == (INTERRUPTED, True)
        assert "running…" not in body_text(interrupted)


async def test_escape_before_the_turn_is_in_flight_still_stops_it(tmp_path: Path) -> None:
    # The real bridge ignores a cancel() that lands before send() is in flight (e.g. while
    # send() waits for the runtime to boot), so the app must keep asking until TurnDone.
    send_gate = threading.Event()  # holds send() before the backend marks the turn in flight
    backend = FakeBackend(gates={"send": send_gate})
    backend.script(AssistantText("started anyway"), threading.Event())  # only cancel ends it
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "go")
        await wait_until(pilot, lambda: backend.calls_named("send") != [])
        await pilot.press("escape")
        await wait_until(pilot, lambda: backend.calls_named("cancel") != [])
        await wait_until(pilot, lambda: backend.interrupt_pending)
        send_gate.set()  # send() now enters the turn and forgets the early cancel
        await wait_until(pilot, lambda: not app.busy)
        assert notices(app, "info") == [STOPPED]
        assert status(app).startswith("ready │")
        assert notices(app, "error") == []
    assert_off_loop(backend, "cancel")


async def test_turn_events_after_escape_keep_the_stopping_status(tmp_path: Path) -> None:
    cancel_gate, first = threading.Event(), threading.Event()
    backend = FakeBackend(gates={"cancel": cancel_gate})
    backend.script(
        first,
        StepStarted(1, 2),
        Retrying(1, 5, 500.0, "SERVER", "503"),
        Reasoning("still going"),
        AssistantText("late"),
        ToolCallStarted("c1", "bash", "{}"),
        threading.Event(),
    )
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "go")
        await wait_until(pilot, backend.blocked.is_set)
        await pilot.press("escape")
        await wait_until(pilot, lambda: status(app).startswith("stopping… │"))
        first.set()  # the runtime is still emitting while it shuts down
        await wait_until(pilot, lambda: backend.blocked.is_set() and len(backend.callbacks) == 1)
        await pilot.pause()
        assert status(app).startswith("stopping… │")
        cancel_gate.set()
        await wait_until(pilot, lambda: not app.busy)


async def test_stop_works_again_on_a_later_turn(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.script(threading.Event())  # only a cancel ends turn 1
    backend.script(Retrying(1, 5, 10.0, "SERVER", "503"), threading.Event())  # and turn 2
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "one")
        await wait_until(pilot, backend.blocked.is_set)
        await pilot.press("escape")
        await wait_until(pilot, lambda: not app.busy)
        await submit(pilot, app, "two")
        await wait_until(pilot, lambda: status(app).startswith("retrying 1/5 (SERVER)… │"))
        await wait_until(pilot, backend.blocked.is_set)
        await pilot.press("escape")
        await wait_until(pilot, lambda: not app.busy)
        assert notices(app, "info") == [STOPPED, STOPPED]
    assert len(backend.calls_named("cancel")) == 2


async def test_escape_in_the_command_palette_closes_it_without_stopping_the_turn(
    tmp_path: Path,
) -> None:
    backend = FakeBackend()
    backend.script(threading.Event())  # only a cancel ends this turn
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "go")
        await wait_until(pilot, backend.blocked.is_set)
        await pilot.press("ctrl+p")
        await wait_until(pilot, lambda: isinstance(app.screen, CommandPalette))
        await pilot.press("escape")
        await wait_until(pilot, lambda: not isinstance(app.screen, CommandPalette))
        await pilot.pause()
        assert app.busy
        assert backend.calls_named("cancel") == []
        await pilot.press("escape")  # back on the chat, Escape stops the turn
        await wait_until(pilot, lambda: not app.busy)
        assert notices(app, "info") == [STOPPED]


async def test_escape_while_idle_does_nothing(tmp_path: Path) -> None:
    backend = FakeBackend()
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await pilot.press("escape")
        await pilot.pause()
        assert status(app).startswith("ready │")
        assert chat_items(app, Notice) == []
    assert backend.calls_named("cancel") == []


async def test_failed_stop_reports_error_and_can_be_retried(tmp_path: Path) -> None:
    backend = FakeBackend(fail={"cancel": RuntimeError("kill [/] failed")})
    backend.script(threading.Event())
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "go")
        await wait_until(pilot, backend.blocked.is_set)
        await pilot.press("escape")
        await wait_until(pilot, lambda: notices(app, "error") != [])
        assert notices(app, "error") == ["stopping the turn failed: kill [/] failed"]
        await wait_until(pilot, lambda: status(app).startswith("thinking… "))
        assert app.busy
        await pilot.press("escape")
        await wait_until(pilot, lambda: len(backend.calls_named("cancel")) == 2)


async def test_ctrl_n_starts_a_new_conversation(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.script(TitleChanged("Old topic"), AssistantText("old reply"), UsageReported(70, 7, 0))
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "old question")
        await wait_until(pilot, lambda: len(backend.sent) == 1 and not app.busy)
        assert status(app).endswith("tokens in 70 · out 7")
        await pilot.press("ctrl+n")
        await wait_until(pilot, lambda: notices(app, "info") == ["new conversation"])
        assert list(app.query_one("#chat").children) == chat_items(app, Notice)
        assert app.sub_title == "new conversation"
        assert status(app) == f"ready │ {MODEL} · {PROFILE} │ tokens in 0 · out 0"
        assert app.focused is app.query_one("#prompt", PromptArea)
    assert_off_loop(backend, "new_conversation")
    assert backend.calls_named("cancel") == []


async def test_ctrl_n_focuses_the_prompt_after_a_click_in_the_chat(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.script(ToolCallStarted("c1", "bash", "{}"), ToolFinished("c1", "a", False))
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "list")
        await wait_until(pilot, lambda: len(backend.sent) == 1 and not app.busy)
        (block,) = chat_items(app, ToolCallBlock)
        await pilot.click(block.query_one(CollapsibleTitle))  # expanding a block takes focus
        assert app.focused is not app.prompt
        await pilot.press("ctrl+n")
        await wait_until(pilot, lambda: notices(app, "info") == ["new conversation"])
        assert app.focused is app.prompt
        await pilot.press("h", "i")
        assert app.prompt.text == "hi"


async def test_ctrl_n_while_busy_stops_first_then_resets(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.script(AssistantText("partial"), threading.Event())
    backend.script(AssistantText("fresh"))
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "long task")
        await wait_until(pilot, backend.blocked.is_set)
        await pilot.press("ctrl+n")
        await wait_until(pilot, lambda: notices(app, "info") == ["new conversation"])
        assert not app.busy
        assert chat_items(app, AssistantMessage) == []
        assert status(app).startswith("ready │")
        await submit(pilot, app, "next")
        await wait_until(pilot, lambda: len(chat_items(app, AssistantMessage)) == 1)
        assert chat_items(app, AssistantMessage)[0].source == "fresh"
    names = [call.name for call in backend.calls if call.name in ("cancel", "new_conversation")]
    assert names == ["cancel", "new_conversation"]


async def test_failed_new_conversation_keeps_the_chat(tmp_path: Path) -> None:
    backend = FakeBackend(fail={"new_conversation": RuntimeError("runtime gone")})
    backend.script(AssistantText("keep me"))
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "hi")
        await wait_until(pilot, lambda: len(backend.sent) == 1 and not app.busy)
        await pilot.press("ctrl+n")
        await wait_until(pilot, lambda: notices(app, "error") != [])
        assert notices(app, "error") == ["starting a new conversation failed: runtime gone"]
        assert [m.source for m in chat_items(app, AssistantMessage)] == ["keep me"]
