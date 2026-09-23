"""Conversation widgets: plain-text attributes for assertions and markup-safe rendering."""

from __future__ import annotations

import unicodedata

import pytest
from textual import on
from textual.app import App, ComposeResult
from textual.events import Key
from textual.widget import Widget
from textual.widgets import Static

from dstui.widgets import (
    AssistantMessage,
    Notice,
    PromptArea,
    ReasoningBlock,
    StatusBar,
    ToolCallBlock,
    UserMessage,
    safe,
)
from tests.helpers_ui import MARKUP, SIZE, body_text, wait_until


class Host(App[None]):
    """Mounts the given widgets so they render like they do in the chat."""

    def __init__(self, *widgets: Widget) -> None:
        super().__init__()
        self._widgets = widgets

    def compose(self) -> ComposeResult:
        yield from self._widgets


def test_safe_keeps_tabs_newlines_and_unicode() -> None:
    text = "a\tb\nc — ünïcødé 漢字 🙂 \u00a0 [/]"
    assert safe(text) == text


def test_safe_removes_every_other_control_character() -> None:
    # Unicode "Cc" is exactly C0, DEL and C1; ESC, CSI (0x9b) and BEL are among them.
    controls = [chr(c) for c in range(0x100) if unicodedata.category(chr(c)) == "Cc"]
    kept = [c for c in controls if safe(f"a{c}b") != "ab"]
    assert kept == ["\t", "\n"]


def test_safe_removes_escape_sequence_introducers_and_crlf_carriage_returns() -> None:
    assert safe("x\x1b]52;c;QUFB\x1b\\y\x1b[2Jz\x9b1m\x07") == "x]52;c;QUFB\\y[2Jz1m"
    assert safe("one\r\ntwo\r\n") == "one\ntwo\n"


async def test_status_bar_never_writes_control_sequences_to_the_terminal() -> None:
    bar = StatusBar()
    app = Host(bar)
    async with app.run_test(size=SIZE) as pilot:
        bar.set_text("retrying (\x1b]0;PWNED\x1b\\\x9b2J)")
        await pilot.pause()
        screen = app.screen._compositor.render_update(full=True).render_segments(app.console)
        assert "retrying (]0;PWNED\\2J)" in screen
        assert "\x1b]" not in screen
        assert "\x9b" not in screen


async def test_user_message_keeps_text_and_renders_markup_literally() -> None:
    message = UserMessage(MARKUP)
    async with Host(message).run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert message.text == MARKUP
        assert MARKUP in message.visual.plain


@pytest.mark.parametrize("level", ["info", "warning", "error"])
async def test_notice_has_level_class_and_literal_text(level: str) -> None:
    notice = Notice(MARKUP, level)
    async with Host(notice).run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert notice.text == MARKUP
        assert notice.level == level
        assert notice.has_class(f"-{level}")
        assert MARKUP in notice.visual.plain


def test_notice_rejects_unknown_level() -> None:
    with pytest.raises(ValueError, match="debug"):
        Notice("x", "debug")


async def test_status_bar_shows_text_literally() -> None:
    bar = StatusBar()
    async with Host(bar).run_test(size=SIZE) as pilot:
        bar.set_text(MARKUP)
        await pilot.pause()
        assert bar.text == MARKUP
        assert bar.visual.plain == MARKUP


async def test_assistant_message_renders_markdown_source() -> None:
    source = f"**hello** {MARKUP}\n\n- one\n- two"
    message = AssistantMessage(source)
    async with Host(message).run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert message.source == source
        blocks = [type(block).__name__ for block in message.children]
        assert blocks == ["MarkdownParagraph", "MarkdownBulletList"]
        paragraph = message.children[0]
        assert isinstance(paragraph, Static)
        assert "[bold]x[/] and [/]" in paragraph.visual.plain


async def test_reasoning_block_is_collapsed_and_literal() -> None:
    block = ReasoningBlock(MARKUP)
    async with Host(block).run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert block.text == MARKUP
        assert block.collapsed
        assert str(block.title) == "thinking"
        assert body_text(block) == MARKUP


async def test_tool_call_block_shows_name_arguments_and_running() -> None:
    block = ToolCallBlock("call-1", MARKUP, '{"command": "[/]"}')
    async with Host(block).run_test(size=SIZE) as pilot:
        await pilot.pause()
        assert (block.call_id, block.tool_name) == ("call-1", MARKUP)
        assert block.arguments == '{"command": "[/]"}'
        assert block.output is None
        assert block.is_error is False
        assert block.collapsed
        assert str(block.title) == f"tool: {MARKUP}"
        assert '{"command": "[/]"}' in body_text(block)
        assert "running…" in body_text(block)


async def test_tool_call_block_finish_shows_output() -> None:
    block = ToolCallBlock("call-1", "bash", '{"command": "ls"}')
    async with Host(block).run_test(size=SIZE) as pilot:
        block.finish(f"out {MARKUP}", is_error=False)
        await pilot.pause()
        assert block.output == f"out {MARKUP}"
        assert block.is_error is False
        assert not block.has_class("-error")
        assert f"out {MARKUP}" in body_text(block)
        assert "running…" not in body_text(block)
        assert '{"command": "ls"}' in body_text(block)


async def test_tool_call_block_finish_marks_error() -> None:
    block = ToolCallBlock("call-2", "bash", "{}")
    async with Host(block).run_test(size=SIZE) as pilot:
        block.finish("permission denied", is_error=True)
        await pilot.pause()
        assert block.is_error is True
        assert block.has_class("-error")
        assert str(block.title) == "tool: bash (error)"
        assert "permission denied" in body_text(block)


class PromptHost(App[None]):
    """Records every PromptArea.Submitted routed by ``@on(..., "#prompt")``."""

    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[str] = []

    def compose(self) -> ComposeResult:
        yield PromptArea(id="prompt")

    def on_mount(self) -> None:
        self.query_one(PromptArea).focus()

    @on(PromptArea.Submitted, "#prompt")
    def record(self, message: PromptArea.Submitted) -> None:
        self.submitted.append(message.text)


async def test_prompt_enter_submits_and_newline_keys_insert_newlines() -> None:
    app = PromptHost()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press("a", "b", "ctrl+j", "c", "shift+enter", "d", "enter")
        await pilot.pause()
        assert app.submitted == ["ab\nc\nd"]
        assert app.query_one(PromptArea).text == "ab\nc\nd"  # the app decides when to clear


async def test_prompt_keeps_the_order_of_keys_that_arrive_in_one_burst() -> None:
    # One terminal read can carry many keys (typing over a slow ssh link, xdotool, a paste
    # without bracketed paste): Enter and Ctrl+J must not overtake the characters before them.
    app = PromptHost()
    async with app.run_test(size=SIZE) as pilot:
        for key, character in [*((c, c) for c in "ab"), ("ctrl+j", "\n"), ("c", "c")]:
            app.post_message(Key(key, character))
        app.post_message(Key("enter", "\r"))
        prompt = app.query_one(PromptArea)
        await wait_until(pilot, lambda: len(prompt.text) == len("ab\nc"))  # every key is in
        await pilot.pause()
        assert app.submitted == ["ab\nc"]
        assert prompt.text == "ab\nc"


@pytest.mark.parametrize("keys", [(), ("space", "ctrl+j", "space")])
async def test_prompt_ignores_blank_input(keys: tuple[str, ...]) -> None:
    app = PromptHost()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.press(*keys, "enter")
        await pilot.pause()
        assert app.submitted == []
