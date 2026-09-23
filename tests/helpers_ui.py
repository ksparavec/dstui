"""UI test helpers shared by the app, widget and end-to-end tests."""

from __future__ import annotations

import time
from collections.abc import Callable

from textual.app import App
from textual.containers import VerticalScroll
from textual.pilot import Pilot
from textual.widget import Widget
from textual.widgets import Collapsible, Static

from dstui.widgets import Notice, StatusBar

SIZE = (100, 30)
MARKUP = "[bold]x[/] and [/]"  # must render literally and never raise MarkupError
# User-visible notices, pinned here as expected texts (not imported from dstui).
STOPPED = "stopped — the runtime was restarted; conversation context was lost"
# After the runtime died on its own (true even if no conversation had started yet).
RESTARTED = (
    "the agent runtime will restart with your next message; "
    "any earlier conversation context is lost"
)


async def wait_until(
    pilot: Pilot[None], predicate: Callable[[], object], timeout: float = 5.0
) -> None:
    """Poll ``predicate`` while letting the app process messages; fail after ``timeout``."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await pilot.pause(0.02)


def status(app: App[None]) -> str:
    return app.query_one("#status", StatusBar).text


def chat_items[W: Widget](app: App[None], kind: type[W]) -> list[W]:
    return list(app.query_one("#chat", VerticalScroll).query(kind))


def notices(app: App[None], level: str) -> list[str]:
    return [notice.text for notice in chat_items(app, Notice) if notice.level == level]


def body_text(box: Collapsible) -> str:
    """Plain text of the Static inside a Collapsible (not its title)."""
    return box.query_one(Collapsible.Contents).query_one(Static).visual.plain
