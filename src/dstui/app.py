"""The Textual chat application."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic

from deepseek_harness.errors import TransportClosedError
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Footer, Header

from dstui import events
from dstui.bridge import AgentBackend, TurnOutcome
from dstui.config import Settings
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

log = logging.getLogger(__name__)

NEW_CONVERSATION = "new conversation"
STATUS_TICK_S = 0.5
CANCEL_RETRY_S = 0.5  # the backend ignores a cancel before send() is in flight
STOPPED = "stopped — the runtime was restarted; conversation context was lost"
RESTARTED = (  # the bridge resets a runtime that died; also true before any conversation
    "the agent runtime will restart with your next message; "
    "any earlier conversation context is lost"
)
API_KEY_MISSING = "DEEPSEEK_API_KEY is not set: export it and restart dstui to chat."
TRUNCATED = "reply truncated: the model reached its max-tokens limit"
INTERRUPTED = "interrupted: the turn ended before the tool finished"
MODEL_OUTPUT = (events.Reasoning, events.AssistantText, events.ToolCallStarted)
ERROR_HINTS = {
    "MISSING_CREDENTIAL": "set DEEPSEEK_API_KEY and restart dstui",
    "AUTH": "the API key was rejected",
    "QUOTA": "insufficient balance: top up your DeepSeek account",
    "TRANSPORT": "cannot reach the DeepSeek API: check your network",
}


def describe(error: BaseException) -> str:
    return str(error) or type(error).__name__


def describe_turn_error(code: str | None, message: str | None) -> str:
    """``model error CODE: message``, plus a friendly hint for the well-known codes."""
    code = code or "UNKNOWN"
    text = f"model error {code}: {message or 'no details'}"
    hint = ERROR_HINTS.get(code)
    return f"{text} ({hint})" if hint else text


@dataclass
class StartDone(Message):
    """The boot worker finished ``backend.start()`` (``error`` is None on success)."""

    error: Exception | None


@dataclass
class AgentEvent(Message):
    """A UI event from the turn worker, tagged with its turn number."""

    turn: int
    event: events.UiEvent


@dataclass
class TurnDone(Message):
    """The turn worker finished: ``outcome`` on return, ``error`` if ``send`` raised."""

    turn: int
    outcome: TurnOutcome | None
    error: Exception | None


class DsTuiApp(App[None]):
    """Chat with a DeepSeek Harness agent."""

    TITLE = "dstui"
    SUB_TITLE = NEW_CONVERSATION
    CSS = "#chat { height: 1fr; padding: 0 1; }"
    BINDINGS = [  # noqa: RUF012 - Textual's declared type for BINDINGS
        Binding("escape", "stop", "Stop", priority=True),
        Binding("ctrl+n", "new_conversation", "New", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),  # re-declared to show in the Footer
    ]

    def __init__(self, backend: AgentBackend, settings: Settings) -> None:
        super().__init__()
        self.backend = backend
        self.settings = settings
        self.turn = 0
        self.busy = False
        self._stopping = False
        self._reset_after_turn = False
        self._phase = "starting…"
        self._since = monotonic()
        self._tools: dict[str, ToolCallBlock] = {}
        self._tokens_in = 0
        self._tokens_out = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="chat")
        yield StatusBar(id="status")
        yield PromptArea(id="prompt", placeholder="Message (Enter to send, Ctrl+J for newline)")
        yield Footer()

    @property
    def chat(self) -> VerticalScroll:
        return self.query_one("#chat", VerticalScroll)

    @property
    def prompt(self) -> PromptArea:
        return self.query_one("#prompt", PromptArea)

    async def on_mount(self) -> None:
        self.chat.anchor()
        self._show_phase("starting…")
        if self.settings.needs_deepseek_key and not self.settings.api_key_set:
            await self._notice(API_KEY_MISSING, "warning")
        self.set_interval(STATUS_TICK_S, self._refresh_status)
        self.prompt.focus()
        self._boot()

    async def on_unmount(self) -> None:
        # Unblocks a turn still running in a worker thread, so the runtime never outlives us.
        try:
            await asyncio.to_thread(self.backend.close)
        except Exception:
            log.exception("closing the agent backend failed")

    @work(thread=True, exclusive=True, group="boot", exit_on_error=False)
    def _boot(self) -> None:
        try:
            self.backend.start()
        except Exception as error:
            log.exception("the agent runtime failed to start")
            self.post_message(StartDone(error))
        else:
            self.post_message(StartDone(None))

    @on(StartDone)
    async def _on_start_done(self, message: StartDone) -> None:
        if message.error is not None:
            await self._notice(f"could not start the agent: {describe(message.error)}", "error")
        if not self.busy:  # a turn started meanwhile owns the status
            self._show_phase("ready")

    @on(PromptArea.Submitted, "#prompt")
    async def _on_submit(self, message: PromptArea.Submitted) -> None:
        if self.busy or not message.text.strip():
            return
        text = message.text.strip("\n")  # keeps the indentation of pasted code
        self.prompt.clear()
        self.turn += 1
        self._set_busy(True)
        self.chat.scroll_end(animate=False)  # re-anchors a chat the user scrolled up
        await self.chat.mount(UserMessage(text))
        self._since = monotonic()
        self._show_phase("thinking")
        self._run_turn(self.turn, text)

    @work(thread=True, exclusive=True, group="agent", exit_on_error=False)
    def _run_turn(self, turn: int, text: str) -> None:
        def forward(event: events.UiEvent) -> None:  # runs on this worker thread
            self.post_message(AgentEvent(turn, event))

        try:
            outcome = self.backend.send(text, forward)
        except Exception as error:
            log.exception("agent turn failed")
            self.post_message(TurnDone(turn, None, error))
        else:
            self.post_message(TurnDone(turn, outcome, None))

    @on(AgentEvent)
    async def _on_agent_event(self, message: AgentEvent) -> None:
        if message.turn != self.turn:
            return  # a late event from a turn that already ended
        if isinstance(message.event, MODEL_OUTPUT):
            self._show_turn_phase("thinking")  # the model answered, so any retry is over
        match message.event:
            case events.Reasoning(text=text):
                await self.chat.mount(ReasoningBlock(text))
            case events.AssistantText(text=text):
                await self.chat.mount(AssistantMessage(text))
            case events.ToolCallStarted(call_id=call_id, name=name, arguments=arguments):
                self._tools[call_id] = ToolCallBlock(call_id, name, arguments)
                await self.chat.mount(self._tools[call_id])
            case events.ToolFinished(call_id=call_id, output=output, is_error=is_error):
                if block := self._tools.get(call_id):
                    block.finish(output, is_error)
            case events.StepStarted():
                self._since = monotonic()
                self._show_turn_phase("thinking")
            case events.Retrying(attempt=attempt, max_retries=max_retries, code=code):
                self._show_turn_phase(f"retrying {attempt}/{max_retries} ({code})…")
            case events.TurnFinished(kind="error", code=code, message=text):
                await self._notice(describe_turn_error(code, text), "error")
            case events.TurnFinished(kind="max-tokens"):
                await self._notice(TRUNCATED, "warning")
            case events.TitleChanged(title=title):
                self.sub_title = safe(title)
            case events.UsageReported(input_tokens=tokens_in, output_tokens=tokens_out):
                self._tokens_in += tokens_in
                self._tokens_out += tokens_out
                self._refresh_status()

    @on(TurnDone)
    async def _on_turn_done(self, message: TurnDone) -> None:
        if message.turn != self.turn:
            return
        self._interrupt_tools()
        if message.error is not None:
            await self._notice(f"agent runtime error: {describe(message.error)}", "error")
            if isinstance(message.error, TransportClosedError):  # the runtime died
                await self._context_lost(RESTARTED)
        elif message.outcome is not None and message.outcome.cancelled:
            await self._context_lost(STOPPED)
        self._set_busy(False)
        self._show_phase("ready")
        if self._reset_after_turn:
            self._reset_after_turn = False
            await self._new_conversation()

    def _interrupt_tools(self) -> None:
        """Mark the turn's unfinished tool calls; call ids are only valid within one turn."""
        for block in self._tools.values():
            if block.output is None:
                block.finish(INTERRUPTED, is_error=True)
        self._tools.clear()

    async def _context_lost(self, text: str) -> None:
        await self._notice(text, "info")
        self.sub_title = NEW_CONVERSATION

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self._stopping = False
        self.prompt.disabled = busy
        if not busy:
            self.prompt.focus()
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "stop" and (not self.busy or self.screen.is_modal):
            return None  # shown dimmed; Escape falls through (e.g. closes the command palette)
        return True

    def action_stop(self) -> None:
        if self.busy and not self._stopping:
            self._stopping = True
            self._show_phase("stopping…")
            # An async worker keeps the UI live while a slow runtime shutdown blocks a thread.
            self.run_worker(self._cancel_turn(self.turn), group="stop", exit_on_error=False)

    async def _cancel_turn(self, turn: int) -> None:
        # The turn worker then returns cancelled, and TurnDone ends the turn. A cancel that
        # lands before the worker's send() is in flight is a no-op, so repeat until TurnDone.
        while self.busy and self.turn == turn:
            if not await self._off_loop(self.backend.cancel, "stopping the turn"):
                self._stopping = False  # let the user try again
                self._show_phase("thinking")
                return
            await asyncio.sleep(CANCEL_RETRY_S)

    async def action_new_conversation(self) -> None:
        if self.busy:  # stop first; TurnDone then starts the new conversation
            self._reset_after_turn = True
            self.action_stop()
        else:
            await self._new_conversation()

    async def _new_conversation(self) -> None:
        if not await self._off_loop(self.backend.new_conversation, "starting a new conversation"):
            return
        await self.chat.remove_children()
        self._tokens_in = self._tokens_out = 0
        self.sub_title = NEW_CONVERSATION
        self._refresh_status()
        await self._notice(NEW_CONVERSATION, "info")
        self.prompt.focus()  # focus may have been on a chat widget that was just removed

    async def _off_loop(self, call: Callable[[], None], what: str) -> bool:
        """Run a blocking backend call in a thread; report a failure as an error notice."""
        try:
            await asyncio.to_thread(call)
        except Exception as error:
            log.exception("%s failed", what)
            await self._notice(f"{what} failed: {describe(error)}", "error")
            return False
        return True

    async def _notice(self, text: str, level: str) -> None:
        await self.chat.mount(Notice(text, level))

    def _show_turn_phase(self, phase: str) -> None:
        if not self._stopping:  # "stopping…" stays until the turn ends
            self._show_phase(phase)

    def _show_phase(self, phase: str) -> None:
        self._phase = phase
        self._refresh_status()

    def _refresh_status(self) -> None:
        if not self.is_running:  # a timer tick can land while shutdown removes the widgets
            return
        settings = self.settings
        phase = self._phase
        if phase == "thinking":
            phase = f"thinking… {int(monotonic() - self._since)}s"
        tokens = f"tokens in {self._tokens_in:,} · out {self._tokens_out:,}"
        text = f"{phase} │ {settings.model} · {settings.profile} │ {tokens}"
        self.query_one("#status", StatusBar).set_text(text)
