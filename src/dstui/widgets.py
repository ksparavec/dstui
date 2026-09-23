"""Conversation widgets. Untrusted text is rendered without markup parsing or control chars."""

from __future__ import annotations

from dataclasses import dataclass

from textual.binding import Binding
from textual.content import Content
from textual.message import Message
from textual.widgets import Collapsible, Markdown, Static, TextArea

NOTICE_LEVELS = ("info", "warning", "error")
# C0 controls except tab and newline, DEL and C1: Textual would pass ESC/CSI/OSC to the terminal.
_CONTROL_CHARS = dict.fromkeys((*range(0x00, 0x09), *range(0x0B, 0x20), *range(0x7F, 0xA0)))


def safe(text: str) -> str:
    """``text`` without terminal control characters (removed, so CRLF becomes plain LF)."""
    return text.translate(_CONTROL_CHARS)


class UserMessage(Static):
    """The user's prompt, echoed locally when it is submitted."""

    DEFAULT_CSS = "UserMessage { margin: 1 0 0 0; color: $accent; text-style: bold; }"

    def __init__(self, text: str) -> None:
        text = safe(text)
        super().__init__(Content(f"> {text}"), markup=False)
        self.text = text


class AssistantMessage(Markdown):
    """Assistant reply text; Markdown never interprets Textual markup. ``.source`` is the text."""

    DEFAULT_CSS = "AssistantMessage { margin: 1 0 0 0; }"

    def __init__(self, markdown: str) -> None:
        super().__init__(safe(markdown))


class ReasoningBlock(Collapsible):
    """The model's reasoning for one step, collapsed by default."""

    DEFAULT_CSS = "ReasoningBlock { color: $text-muted; }"

    def __init__(self, text: str) -> None:
        text = safe(text)
        super().__init__(Static(text, markup=False), title=Content("thinking"), collapsed=True)
        self.text = text


class ToolCallBlock(Collapsible):
    """One tool call: its arguments while running, then its output (``finish``)."""

    DEFAULT_CSS = "ToolCallBlock.-error CollapsibleTitle { color: $error; }"

    def __init__(self, call_id: str, tool_name: str, arguments: str) -> None:
        body = Static(markup=False)
        tool_name = safe(tool_name)
        super().__init__(body, title=Content(f"tool: {tool_name}"), collapsed=True)
        self._body = body
        self.call_id = call_id
        self.tool_name = tool_name
        self.arguments = safe(arguments)
        self.output: str | None = None
        self.is_error = False
        self._body.update(Content(self._body_text()))

    def finish(self, output: str, is_error: bool) -> None:
        self.output = safe(output)
        self.is_error = is_error
        if is_error:
            self.add_class("-error")
            self.title = Content(f"tool: {self.tool_name} (error)")
        self._body.update(Content(self._body_text()))

    def _body_text(self) -> str:
        if self.output is None:
            return f"{self.arguments}\n\nrunning…"
        label = "error" if self.is_error else "output"
        return f"{self.arguments}\n\n{label}:\n{self.output}"


class Notice(Static):
    """A one-off info, warning or error line in the conversation."""

    DEFAULT_CSS = """
    Notice { margin: 1 0 0 0; color: $text-muted; }
    Notice.-warning { color: $warning; }
    Notice.-error { color: $error; }
    """

    def __init__(self, text: str, level: str = "info") -> None:
        if level not in NOTICE_LEVELS:
            raise ValueError(f"unknown notice level {level!r}")
        text = safe(text)
        super().__init__(Content(text), markup=False, classes=f"-{level}")
        self.text = text
        self.level = level


class StatusBar(Static):
    """One-line status: phase, model, profile and token totals."""

    DEFAULT_CSS = "StatusBar { height: 1; padding: 0 1; background: $panel; }"

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__("", markup=False, id=id)
        self.text = ""

    def set_text(self, text: str) -> None:
        text = safe(text)
        if text != self.text:
            self.text = text
            self.update(Content(text))


class PromptArea(TextArea):
    """Multi-line prompt: Enter submits, Ctrl+J (or Shift+Enter) inserts a newline."""

    DEFAULT_CSS = "PromptArea { height: auto; max-height: 10; }"
    BINDINGS = [  # noqa: RUF012 - Textual's declared type for BINDINGS
        # priority: checked before TextArea's key handler turns "enter" into "\n"
        Binding("enter", "submit", "Send", priority=True),
        Binding("shift+enter,ctrl+j", "newline", "Newline", show=False, priority=True),
    ]

    @dataclass
    class Submitted(Message):
        prompt_area: PromptArea
        text: str

        @property
        def control(self) -> PromptArea:  # lets @on(PromptArea.Submitted, "#prompt") match
            return self.prompt_area

    # A priority binding runs on the App as soon as it reads the key, which can be before this
    # widget has inserted the keys that arrived just before it (one terminal read can carry
    # many keys). So the actions queue up behind those keys instead of acting at once.
    def action_submit(self) -> None:
        self.call_later(self._submit)

    def action_newline(self) -> None:
        self.call_later(self.insert, "\n")

    def _submit(self) -> None:
        if self.text.strip():
            self.post_message(self.Submitted(self, self.text))
