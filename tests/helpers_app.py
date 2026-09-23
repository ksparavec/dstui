"""Helpers for driving ``DsTuiApp`` against the scriptable ``FakeBackend``."""

from __future__ import annotations

import threading
from pathlib import Path

from textual.pilot import Pilot

from dstui.app import DsTuiApp
from dstui.config import Settings
from dstui.widgets import PromptArea
from tests.fake_backend import FakeBackend
from tests.helpers_ui import status, wait_until

MODEL = "deepseek-v4-pro"
PROFILE = "sdk-minimal"
INTERRUPTED = "interrupted: the turn ended before the tool finished"


def make_settings(tmp_path: Path, *, api_key_set: bool = True) -> Settings:
    return Settings(
        workspace=tmp_path,
        data_dir=tmp_path / "data",
        profile=PROFILE,
        model=MODEL,
        api_key_set=api_key_set,
    )


def make_app(tmp_path: Path, backend: FakeBackend, *, api_key_set: bool = True) -> DsTuiApp:
    return DsTuiApp(backend, make_settings(tmp_path, api_key_set=api_key_set))


def assert_off_loop(backend: FakeBackend, name: str) -> None:
    """``name`` was called at least once, and never on the event loop (this) thread."""
    calls = backend.calls_named(name)
    assert calls, f"backend.{name}() was not called"
    loop_thread = threading.get_ident()
    assert all(call.thread_id != loop_thread for call in calls)


async def submit(pilot: Pilot[None], app: DsTuiApp, text: str) -> None:
    prompt = app.query_one("#prompt", PromptArea)
    prompt.focus()
    prompt.text = text
    await pilot.press("enter")


async def ready_app(pilot: Pilot[None], app: DsTuiApp) -> None:
    await wait_until(pilot, lambda: status(app).startswith("ready"))


class FakeClock:
    """Stands in for ``dstui.app.monotonic`` so elapsed-time tests need no sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now
