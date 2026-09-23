"""The shipped entry point in a real pseudo-terminal: real driver, real key bytes, real runtime.

Every other UI test uses ``App.run_test`` (headless driver, injected key names); this one
proves that ``python -m dstui`` starts, decodes real key bytes and quits in a terminal.
"""

from __future__ import annotations

import os
import re
import select
import signal
import struct
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.fake_deepseek import FakeDeepSeek, conversation, text_reply

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs a Linux pty"),
]

ROWS, COLUMNS = 30, 110
TOTAL_S = 60.0  # hard bound for the whole session; locally it takes about 2 s
READ_S = 0.05
KEY_GAP_S = 0.15  # pause after each write, reading what the app draws meanwhile
REPLY = "pty-reply-ok"
CTRL_J, ENTER, CTRL_Q = b"\n", b"\r", b"\x11"  # the bytes a terminal sends for these keys
ANSI = re.compile(
    rb"\x1b\[[0-9;?<>=]*[ -/]*[@-~]"  # CSI
    rb"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    rb"|\x1b[()][0-9A-Za-z]|\x1b[=>78]"
)


class Terminal:
    """The master side of the pty: collects everything the app draws, within one deadline."""

    def __init__(self, fd: int, deadline: float) -> None:
        self.fd = fd
        self.deadline = deadline
        self.output = bytearray()

    @property
    def text(self) -> str:
        """Everything drawn so far, without escape sequences or line breaks."""
        plain = ANSI.sub(b"", bytes(self.output)).decode("utf-8", "replace")
        return plain.replace("\r", "").replace("\n", "")

    def press(self, *keys: bytes) -> None:
        """Write each of ``keys`` separately (one ``bytes`` can hold several keys)."""
        for key in keys:
            os.write(self.fd, key)
            until = time.monotonic() + KEY_GAP_S
            while time.monotonic() < until:
                self.read()

    def read(self, timeout: float = READ_S) -> bytes:
        """What arrives within ``timeout`` (an unread pty blocks the app); b"" if nothing."""
        if not select.select([self.fd], [], [], timeout)[0]:
            return b""
        try:
            chunk = os.read(self.fd, 65536)
        except OSError:  # EIO: every process closed the terminal; poll gently from now on
            time.sleep(timeout)
            return b""
        self.output.extend(chunk)
        return chunk

    def wait_until(self, condition: Callable[[], bool], what: str) -> None:
        while not condition():
            if time.monotonic() > self.deadline:
                raise AssertionError(f"timed out waiting for {what}; screen: {self.text[-1500:]!r}")
            self.read()

    def drain(self) -> None:
        """Collect what the app wrote just before it exited (e.g. a traceback)."""
        while self.read(0.2):
            pass


def spawn_dstui(
    args: list[str], env: dict[str, str], cwd: Path
) -> tuple[subprocess.Popen[bytes], int]:
    """Start ``python -m dstui`` on a fresh ROWS x COLUMNS pty; return it and the master fd."""
    import fcntl  # POSIX only: imported here so collection works everywhere
    import termios

    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLUMNS, 0, 0))
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "dstui", *args],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            cwd=cwd,
            start_new_session=True,  # its own process group, so cleanup can kill all of it
        )
    finally:
        os.close(slave)
    return process, master


def last_user_message(fake: FakeDeepSeek) -> str | None:
    requests = fake.requests
    users = (
        [text for role, text in conversation(requests[-1]) if role == "user"] if requests else []
    )
    return users[-1] if users else None


def test_dstui_in_a_real_terminal_sends_a_multiline_prompt_and_quits_cleanly(
    fake: FakeDeepSeek, tmp_path: Path
) -> None:
    fake.enqueue(text_reply(REPLY))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env = {key: value for key, value in os.environ.items() if key not in ("COLUMNS", "LINES")}
    env |= {
        "DEEPSEEK_API_KEY": "sk-fake",
        "DEEPSEEK_BASE_URL": fake.url,
        "DSTUI_HOME": str(tmp_path / "data"),  # the runtime's DSH_HOME, seen by the leak check
        "TERM": "xterm-256color",
    }
    process, master = spawn_dstui(
        ["-w", str(workspace), "--profile", "sdk-minimal"], env, workspace
    )
    terminal = Terminal(master, deadline=time.monotonic() + TOTAL_S)
    try:
        terminal.wait_until(lambda: "ready" in terminal.text, "the ready status")
        # one write, so one read: Ctrl+J and Enter must not overtake the keys before them
        terminal.press(b"a" + CTRL_J + b"b" + ENTER)
        terminal.wait_until(lambda: last_user_message(fake) == "a\nb", "the prompt 'a\\nb'")
        terminal.wait_until(lambda: REPLY in terminal.text, "the reply on screen")
        terminal.press(CTRL_Q)
        terminal.wait_until(lambda: process.poll() is not None, "dstui to exit after Ctrl+Q")
        terminal.drain()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        os.close(master)

    assert process.returncode == 0
    assert "Traceback" not in terminal.text
    assert len(fake.requests) == 1
