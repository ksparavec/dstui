"""DsTuiApp behaviour, driven through Pilot against the scriptable ``FakeBackend``."""

from __future__ import annotations

import dataclasses
import re
import threading
import time
from pathlib import Path

import pytest
from deepseek_harness.errors import JsonRpcError, TransportClosedError
from textual.containers import VerticalScroll
from textual.events import MouseScrollUp
from textual.widget import Widget
from textual.widgets._footer import FooterKey
from textual.widgets._header import HeaderTitle

from dstui.app import DsTuiApp, TurnDone, describe
from dstui.bridge import TurnOutcome
from dstui.events import (
    AssistantText,
    Reasoning,
    Retrying,
    StepStarted,
    TitleChanged,
    ToolCallStarted,
    ToolFinished,
    TurnFinished,
    UiEvent,
    UsageReported,
)
from dstui.widgets import (
    AssistantMessage,
    Notice,
    PromptArea,
    ReasoningBlock,
    ToolCallBlock,
    UserMessage,
)
from tests.fake_backend import FakeBackend
from tests.helpers_app import (
    MODEL,
    PROFILE,
    FakeClock,
    assert_off_loop,
    make_app,
    make_settings,
    ready_app,
    submit,
)
from tests.helpers_ui import (
    MARKUP,
    RESTARTED,
    SIZE,
    body_text,
    chat_items,
    notices,
    status,
    wait_until,
)


async def test_layout_title_and_startup_status(tmp_path: Path) -> None:
    gate = threading.Event()
    backend = FakeBackend(gates={"start": gate})
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        assert app.query_one("#chat", VerticalScroll).is_anchored
        assert isinstance(app.query_one("#prompt"), PromptArea)
        assert app.title == "dstui"
        assert app.sub_title == "new conversation"
        await wait_until(pilot, lambda: backend.calls_named("start") != [])
        assert status(app).startswith("starting…")
        assert MODEL in status(app)
        assert PROFILE in status(app)
        gate.set()
        await wait_until(pilot, lambda: status(app).startswith("ready"))
        assert MODEL in status(app)
        assert PROFILE in status(app)
    assert_off_loop(backend, "start")


async def test_the_chat_gets_the_room_and_the_prompt_grows_to_ten_rows(tmp_path: Path) -> None:
    app = make_app(tmp_path, FakeBackend())
    async with app.run_test(size=(100, 30)) as pilot:
        await ready_app(pilot, app)
        chat = app.query_one("#chat", VerticalScroll)
        assert app.prompt.region.height <= 3
        assert chat.region.height >= 20
        app.prompt.text = "\n".join(f"line {n}" for n in range(40))
        await wait_until(pilot, lambda: app.prompt.region.height == 10)  # then it scrolls
        await pilot.pause()
        assert app.prompt.region.height == 10
        assert chat.region.height >= 13


@pytest.mark.parametrize("api_key_set", [False, True])
async def test_warns_at_startup_when_api_key_is_missing(tmp_path: Path, api_key_set: bool) -> None:
    backend = FakeBackend()
    app = make_app(tmp_path, backend, api_key_set=api_key_set)
    async with app.run_test(size=SIZE) as pilot:
        await wait_until(pilot, lambda: status(app).startswith("ready"))
        warnings = notices(app, "warning")
        if api_key_set:
            assert warnings == []
        else:
            assert len(warnings) == 1
            assert "DEEPSEEK_API_KEY" in warnings[0]
            assert "not set" in warnings[0]


async def test_no_api_key_warning_for_another_provider(tmp_path: Path) -> None:
    """DEEPSEEK_API_KEY is only read by DeepSeek's own provider."""
    backend = FakeBackend()
    settings = dataclasses.replace(
        make_settings(tmp_path, api_key_set=False), provider="router-ollama", model="qwen3:8b"
    )
    app = DsTuiApp(backend, settings)
    async with app.run_test(size=SIZE) as pilot:
        await wait_until(pilot, lambda: status(app).startswith("ready"))
        assert notices(app, "warning") == []


async def test_start_failure_shows_error_notice_and_app_stays_usable(tmp_path: Path) -> None:
    backend = FakeBackend(fail={"start": FileNotFoundError("runtime binary missing")})
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await wait_until(pilot, lambda: notices(app, "error") != [])
        assert "runtime binary missing" in notices(app, "error")[0]
        await wait_until(pilot, lambda: status(app).startswith("ready"))
        assert not app.query_one("#prompt", PromptArea).disabled
        await submit(pilot, app, "retry please")
        await wait_until(pilot, lambda: backend.sent == ["retry please"] and not app.busy)
        assert status(app).startswith("ready")
        assert len(notices(app, "error")) == 1


async def test_start_finishing_mid_turn_keeps_thinking_status(tmp_path: Path) -> None:
    start_gate, turn_gate = threading.Event(), threading.Event()
    backend = FakeBackend(gates={"start": start_gate})
    backend.script(turn_gate)
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await submit(pilot, app, "early bird")
        await wait_until(pilot, lambda: backend.sent == ["early bird"])
        start_gate.set()
        await wait_until(pilot, lambda: not any(w.group == "boot" for w in app.workers))
        await pilot.pause()  # StartDone was posted before the boot worker finished
        assert status(app).startswith("thinking…")
        turn_gate.set()
        await wait_until(pilot, lambda: status(app).startswith("ready"))


async def test_submit_echoes_prompt_locks_input_and_runs_turn_off_loop(tmp_path: Path) -> None:
    gate = threading.Event()
    backend = FakeBackend()
    backend.script(gate)
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "\n  hello\nworld\n\n")
        await wait_until(pilot, backend.blocked.is_set)
        prompt = app.query_one("#prompt", PromptArea)
        assert [message.text for message in chat_items(app, UserMessage)] == ["  hello\nworld"]
        assert prompt.text == ""
        assert prompt.disabled
        assert app.busy
        assert re.match(r"thinking… \d+s │", status(app))
        assert backend.sent == ["  hello\nworld"]
        gate.set()
        await wait_until(pilot, lambda: not app.busy)
        assert not prompt.disabled
        assert app.focused is prompt
        assert status(app).startswith("ready")
    assert_off_loop(backend, "send")


async def test_submit_keeps_indentation_and_ignores_whitespace_only_input(tmp_path: Path) -> None:
    backend = FakeBackend()
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        prompt = app.query_one("#prompt", PromptArea)
        prompt.post_message(PromptArea.Submitted(prompt, " \n\t "))
        await submit(pilot, app, "    x = 1\n    y = 2\n")
        await wait_until(pilot, lambda: len(backend.sent) == 1 and not app.busy)
        assert backend.sent == ["    x = 1\n    y = 2"]
        assert [message.text for message in chat_items(app, UserMessage)] == backend.sent


async def test_quit_mid_turn_closes_backend_off_loop_and_exits_promptly(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.script(threading.Event())  # never opened: only close() can end this turn
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "long job")
        await wait_until(pilot, backend.blocked.is_set)
        quit_pressed = time.monotonic()
        await pilot.press("ctrl+q")
    assert time.monotonic() - quit_pressed < 2.0
    assert app.return_code == 0
    assert backend.closed.is_set()
    assert_off_loop(backend, "close")


async def test_renders_reasoning_replies_and_tool_calls_in_order(tmp_path: Path) -> None:
    gate = threading.Event()
    backend = FakeBackend()
    backend.script(
        TitleChanged("List files"),
        Reasoning("let me look"),
        AssistantText("Checking."),
        ToolCallStarted("c1", "bash", '{"command": "ls"}'),
        gate,
        ToolFinished("c1", "a.txt", False),
        ToolCallStarted("c2", "bash", '{"command": "cat nope"}'),
        ToolFinished("c2", "No such file", True),
        ToolFinished("unknown-call", "ignored", False),
        AssistantText("**Done**"),
    )
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "list files")
        await wait_until(pilot, lambda: len(chat_items(app, ToolCallBlock)) == 1)
        running = chat_items(app, ToolCallBlock)[0]
        assert running.output is None
        assert str(running.title) == "tool: bash"
        assert app.sub_title == "List files"
        gate.set()
        await wait_until(pilot, lambda: not app.busy)
        kinds = [type(widget).__name__ for widget in app.query_one("#chat").children]
        assert kinds == [
            "UserMessage",
            "ReasoningBlock",
            "AssistantMessage",
            "ToolCallBlock",
            "ToolCallBlock",
            "AssistantMessage",
        ]
        (reasoning,) = chat_items(app, ReasoningBlock)
        assert reasoning.text == "let me look"
        assert reasoning.collapsed
        assert [m.source for m in chat_items(app, AssistantMessage)] == ["Checking.", "**Done**"]
        first, second = chat_items(app, ToolCallBlock)
        assert (first.call_id, first.output, first.is_error) == ("c1", "a.txt", False)
        assert (second.call_id, second.output, second.is_error) == ("c2", "No such file", True)


async def notices_after_one_turn(tmp_path: Path, backend: FakeBackend) -> dict[str, list[str]]:
    """Run one scripted turn to completion; return the notice texts by level."""
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "go")
        await wait_until(pilot, lambda: len(backend.sent) == 1 and not app.busy)
        return {level: notices(app, level) for level in ("info", "warning", "error")}


@pytest.mark.parametrize(
    ("code", "hint"),
    [
        ("MISSING_CREDENTIAL", "set DEEPSEEK_API_KEY"),
        ("AUTH", "key was rejected"),
        ("QUOTA", "balance"),
        ("TRANSPORT", "cannot reach the DeepSeek API"),
    ],
)
async def test_turn_error_shows_code_message_and_hint(tmp_path: Path, code: str, hint: str) -> None:
    backend = FakeBackend()
    backend.script(TurnFinished("error", code, "upstream said no"), outcome=TurnOutcome("error"))
    (text,) = (await notices_after_one_turn(tmp_path, backend))["error"]
    assert code in text
    assert "upstream said no" in text
    assert hint in text


async def test_turn_error_with_other_code_shows_the_message(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.script(TurnFinished("error", "SERVER", "HTTP 503 from provider"))
    shown = await notices_after_one_turn(tmp_path, backend)
    assert shown["error"] == ["model error SERVER: HTTP 503 from provider"]


async def test_turn_error_without_details_still_explains(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.script(TurnFinished("error"))
    shown = await notices_after_one_turn(tmp_path, backend)
    assert shown["error"] == ["model error UNKNOWN: no details"]


async def test_max_tokens_warns_reply_truncated_and_completed_is_silent(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.script(AssistantText("partial"), TurnFinished("max-tokens"), TurnFinished("completed"))
    shown = await notices_after_one_turn(tmp_path, backend)
    (warning,) = shown["warning"]
    assert "reply truncated" in warning
    assert shown["error"] == []
    assert shown["info"] == []


async def test_runtime_crash_says_the_context_is_lost_and_the_next_send_works(
    tmp_path: Path,
) -> None:
    backend = FakeBackend()
    backend.script(
        TitleChanged("Old topic"),
        AssistantText("half"),
        error=TransportClosedError("runtime stdout closed"),
    )
    backend.script(AssistantText("recovered"))
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "first")
        await wait_until(pilot, lambda: len(backend.sent) == 1 and not app.busy)
        assert notices(app, "error") == ["agent runtime error: runtime stdout closed"]
        assert notices(app, "info") == [RESTARTED]
        assert app.sub_title == "new conversation"
        assert status(app).startswith("ready")
        await submit(pilot, app, "second")
        await wait_until(pilot, lambda: len(chat_items(app, AssistantMessage)) == 2)
        assert backend.sent == ["first", "second"]
        await wait_until(pilot, lambda: not app.busy)


@pytest.mark.parametrize(
    "error",
    [RuntimeError("runtime died: exit 137"), JsonRpcError(-32602, "invalid params")],
    ids=["other-exception", "rejected-request"],
)
async def test_other_turn_errors_show_a_notice_and_keep_the_conversation(
    tmp_path: Path, error: Exception
) -> None:
    backend = FakeBackend()
    backend.script(TitleChanged("Topic"), AssistantText("half"), error=error)
    backend.script(AssistantText("recovered"))
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "first")
        await wait_until(pilot, lambda: notices(app, "error") != [])
        assert notices(app, "error") == [f"agent runtime error: {error}"]
        await wait_until(pilot, lambda: not app.busy)
        assert notices(app, "info") == []
        assert app.sub_title == "Topic"
        assert status(app).startswith("ready")
        assert not app.query_one("#prompt", PromptArea).disabled
        await submit(pilot, app, "second")
        await wait_until(pilot, lambda: len(chat_items(app, AssistantMessage)) == 2)
        assert backend.sent == ["first", "second"]
        await wait_until(pilot, lambda: not app.busy)


async def test_events_from_a_stale_turn_are_ignored(tmp_path: Path) -> None:
    gate = threading.Event()
    backend = FakeBackend()
    backend.script(AssistantText("one"))
    backend.script(gate, AssistantText("two"))
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "first")
        await wait_until(pilot, lambda: not app.busy)
        await submit(pilot, app, "second")
        await wait_until(pilot, backend.blocked.is_set)
        late = threading.Thread(target=backend.callbacks[0], args=(AssistantText("stale"),))
        late.start()
        late.join()
        gate.set()  # turn 2's events are queued after the stale one
        await wait_until(pilot, lambda: not app.busy)
        assert [m.source for m in chat_items(app, AssistantMessage)] == ["one", "two"]


async def test_prompt_is_locked_while_a_turn_runs(tmp_path: Path) -> None:
    gate = threading.Event()
    backend = FakeBackend()
    backend.script(gate)
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "first")
        await wait_until(pilot, backend.blocked.is_set)
        prompt = app.query_one("#prompt", PromptArea)
        await submit(pilot, app, "second")  # the disabled prompt cannot take focus or submit
        prompt.post_message(PromptArea.Submitted(prompt, "third"))  # one that slipped through
        await pilot.pause()
        assert app.focused is not prompt
        assert [message.text for message in chat_items(app, UserMessage)] == ["first"]
        gate.set()
        await wait_until(pilot, lambda: not app.busy)
    assert backend.sent == ["first"]


async def test_token_totals_accumulate_across_turns(tmp_path: Path) -> None:
    backend = FakeBackend()
    backend.script(UsageReported(100, 20, 5), AssistantText("a"), UsageReported(50, 10, 0))
    backend.script(UsageReported(1000, 300, 0))
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        assert status(app).endswith("tokens in 0 · out 0")
        await submit(pilot, app, "first")
        await wait_until(pilot, lambda: len(backend.sent) == 1 and not app.busy)
        assert status(app).startswith("ready")
        assert status(app).endswith("tokens in 150 · out 30")
        await submit(pilot, app, "second")
        await wait_until(pilot, lambda: len(backend.sent) == 2 and not app.busy)
        assert status(app).endswith("tokens in 1,150 · out 330")


async def test_thinking_status_counts_seconds_and_resets_on_step_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    monkeypatch.setattr("dstui.app.monotonic", clock)
    first_step, second_step = threading.Event(), threading.Event()
    backend = FakeBackend()
    backend.script(first_step, StepStarted(1, 2), second_step)
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        clock.now += 300.0  # the user reads for five minutes before asking
        await submit(pilot, app, "think hard")
        await wait_until(pilot, backend.blocked.is_set)
        assert status(app).startswith("thinking… 0s │")
        clock.now += 7.4
        await wait_until(pilot, lambda: status(app).startswith("thinking… 7s │"))  # timer tick
        first_step.set()
        await wait_until(pilot, lambda: status(app).startswith("thinking… 0s │"))  # reset
        clock.now += 2.0
        await wait_until(pilot, lambda: status(app).startswith("thinking… 2s │"))
        second_step.set()
        await wait_until(pilot, lambda: status(app).startswith("ready │"))


async def test_retrying_status_until_the_next_step(tmp_path: Path) -> None:
    retried, stepped = threading.Event(), threading.Event()
    backend = FakeBackend()
    backend.script(Retrying(2, 5, 900.0, "SERVER", "HTTP 503"), retried, StepStarted(1, 2), stepped)
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "go")
        await wait_until(pilot, lambda: status(app).startswith("retrying 2/5 (SERVER)… │"))
        assert MODEL in status(app)
        retried.set()
        await wait_until(pilot, lambda: status(app).startswith("thinking… "))
        stepped.set()
        await wait_until(pilot, lambda: status(app).startswith("ready │"))


async def test_a_new_prompt_brings_the_scrolled_up_chat_back_to_the_bottom(tmp_path: Path) -> None:
    long_reply = "\n\n".join(f"paragraph {i} " + "word " * 30 for i in range(15))
    backend = FakeBackend()
    for _ in range(3):
        backend.script(AssistantText(long_reply))
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        chat = app.query_one("#chat", VerticalScroll)
        await submit(pilot, app, "one")
        await wait_until(pilot, lambda: len(backend.sent) == 1 and not app.busy)
        await submit(pilot, app, "two")
        await wait_until(pilot, lambda: len(backend.sent) == 2 and not app.busy)
        await wait_until(pilot, lambda: 0 < chat.max_scroll_y == chat.scroll_y)
        for _ in range(5):  # the mouse wheel over the chat releases its bottom anchor
            chat.post_message(MouseScrollUp(chat, 5, 5, 0, -1, 0, False, False, False))
        await wait_until(pilot, lambda: chat.scroll_y < chat.max_scroll_y)
        await submit(pilot, app, "three")
        await wait_until(pilot, lambda: len(chat_items(app, AssistantMessage)) == 3)
        await wait_until(pilot, lambda: not app.busy)
        await pilot.pause()
        assert (chat.scroll_y, chat.is_vertical_scroll_end) == (chat.max_scroll_y, True)


@pytest.mark.parametrize(
    ("answer", "shown_as"),
    [
        (Reasoning("hmm"), ReasoningBlock),
        (AssistantText("ok"), AssistantMessage),
        (ToolCallStarted("c1", "bash", "{}"), ToolCallBlock),
    ],
    ids=["reasoning", "reply", "tool-call"],
)
async def test_retrying_status_returns_to_thinking_when_the_retry_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: UiEvent, shown_as: type[Widget]
) -> None:
    clock = FakeClock()
    monkeypatch.setattr("dstui.app.monotonic", clock)
    retried, answered = threading.Event(), threading.Event()
    backend = FakeBackend()
    backend.script(Retrying(1, 5, 500.0, "SERVER", "503"), retried, answer, answered)
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "go")
        await wait_until(pilot, lambda: status(app).startswith("retrying 1/5 (SERVER)… │"))
        clock.now += 4.0
        retried.set()
        await wait_until(pilot, lambda: len(chat_items(app, shown_as)) == 1)
        await pilot.pause()
        assert status(app).startswith("thinking… 4s │")  # the step's timer keeps counting
        answered.set()
        await wait_until(pilot, lambda: not app.busy)


async def test_footer_shows_send_stop_new_and_quit(tmp_path: Path) -> None:
    app = make_app(tmp_path, FakeBackend())
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await pilot.pause()
        shown = {key.description: key.key for key in app.query(FooterKey)}
        assert shown["Send"] == "enter"
        assert shown["Stop"] == "escape"
        assert shown["New"] == "ctrl+n"
        assert shown["Quit"] == "ctrl+q"


async def test_untrusted_markup_renders_literally_everywhere(tmp_path: Path) -> None:
    backend = FakeBackend(fail={"start": RuntimeError(f"start {MARKUP}")})
    backend.script(
        TitleChanged(MARKUP),
        Reasoning(MARKUP),
        AssistantText(MARKUP),
        ToolCallStarted("c1", MARKUP, MARKUP),
        ToolFinished("c1", MARKUP, True),
        Retrying(1, 5, 100.0, MARKUP, MARKUP),
        TurnFinished("error", MARKUP, MARKUP),
        error=RuntimeError(MARKUP),
    )
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await wait_until(pilot, lambda: len(notices(app, "error")) == 1)
        await submit(pilot, app, MARKUP)
        await wait_until(pilot, lambda: len(notices(app, "error")) == 3 and not app.busy)
        await pilot.pause()
        (user,) = chat_items(app, UserMessage)
        assert MARKUP in user.visual.plain
        for notice in chat_items(app, Notice):
            assert MARKUP in notice.text
            assert notice.text in notice.visual.plain
        (reasoning,) = chat_items(app, ReasoningBlock)
        assert body_text(reasoning) == MARKUP
        (tool,) = chat_items(app, ToolCallBlock)
        assert str(tool.title) == f"tool: {MARKUP} (error)"
        assert body_text(tool).count(MARKUP) == 2  # arguments and output
        (reply,) = chat_items(app, AssistantMessage)
        assert reply.source == MARKUP
        assert app.query_one(HeaderTitle).content.plain == f"dstui — {MARKUP}"


def rendered(app: DsTuiApp) -> str:
    """The exact text App._display writes to the terminal for a full repaint."""
    return app.screen._compositor.render_update(full=True).render_segments(app.console)


async def test_terminal_control_sequences_never_reach_the_terminal(tmp_path: Path) -> None:
    # OSC 52 (clipboard write), CSI clear screen, a C1 CSI and BEL
    escapes = "\x1b]52;c;QUFB\x1b\\" + "\x1b[2J" + "\x9b" + "\x07"
    backend = FakeBackend()
    backend.script(
        TitleChanged(f"title-before{escapes}title-after"),
        Reasoning(f"reasoning-before{escapes}reasoning-after"),
        AssistantText(f"reply-before{escapes}reply-after"),
        ToolCallStarted("c1", f"name-before{escapes}name-after", f"args-before{escapes}"),
        ToolFinished("c1", f"output-before{escapes}output-after", False),
        TurnFinished("error", "SERVER", f"error-before{escapes}error-after"),
    )
    app = make_app(tmp_path, backend)
    async with app.run_test(size=(120, 60)) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, f"user-before{escapes}user-after")
        await wait_until(pilot, lambda: len(backend.sent) == 1 and not app.busy)
        for block in [*chat_items(app, ReasoningBlock), *chat_items(app, ToolCallBlock)]:
            block.collapsed = False
        await pilot.pause()
        screen = rendered(app)
    for where in ("user", "title", "reasoning", "reply", "name", "output", "error"):
        assert f"{where}-before" in screen
        assert f"{where}-after" in screen
    assert "args-before" in screen
    for payload in ("\x1b]", "\x1b\\", "\x1b[2J", "\x9b", "\x07"):
        assert payload not in screen


async def test_quit_still_exits_when_close_fails(tmp_path: Path) -> None:
    backend = FakeBackend(fail={"close": RuntimeError("shutdown RPC timed out")})
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await pilot.press("ctrl+q")
    assert app.return_code == 0
    assert_off_loop(backend, "close")


async def test_a_stale_turn_done_is_ignored(tmp_path: Path) -> None:
    gate = threading.Event()
    backend = FakeBackend()
    backend.script(gate)
    app = make_app(tmp_path, backend)
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
        await submit(pilot, app, "go")
        await wait_until(pilot, backend.blocked.is_set)
        app.post_message(TurnDone(app.turn - 1, TurnOutcome(None, cancelled=True), None))
        await pilot.pause()
        assert app.busy
        assert notices(app, "info") == []
        gate.set()
        await wait_until(pilot, lambda: not app.busy)


async def test_a_status_tick_while_the_app_shuts_down_is_harmless(tmp_path: Path) -> None:
    # Pilot shuts the app down while its status timer still runs, so a tick can land after
    # the widgets are gone (a flaky NoMatches in any test that ends near a tick).
    app = make_app(tmp_path, FakeBackend())
    async with app.run_test(size=SIZE) as pilot:
        await ready_app(pilot, app)
    app._refresh_status()  # what that late tick calls


def test_describe_falls_back_to_the_exception_type() -> None:
    assert describe(TimeoutError()) == "TimeoutError"
    assert describe(RuntimeError("boom")) == "boom"
