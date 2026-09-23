"""AgentBridge against the REAL SDK and bundled runtime, talking to the fake DeepSeek API."""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from deepseek_harness import Notification, RunResult, Session
from deepseek_harness.errors import JsonRpcError, TransportClosedError

import dstui.bridge
from dstui.bridge import AgentBridge, TurnOutcome
from dstui.config import EFFORTS
from dstui.events import (
    AssistantText,
    Reasoning,
    StatusChanged,
    ToolCallStarted,
    ToolFinished,
    TurnFinished,
    UiEvent,
    map_notification,
)
from tests.conftest import HarnessConfigFactory, runtime_pids_under
from tests.fake_deepseek import (
    FakeDeepSeek,
    JsonObject,
    Reply,
    auth_error_reply,
    conversation,
    error_reply,
    text_reply,
    tool_call_reply,
    tool_names,
)

pytestmark = pytest.mark.e2e

WAIT_S = 15.0
POLL_S = 0.02
STOP_S = 3.0  # cancel/close measured at 0.05-0.1 s; the bound only has to catch "never"

type BridgeFactory = Callable[..., AgentBridge]


def wait_for(predicate: Callable[[], object], timeout: float = WAIT_S) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        time.sleep(POLL_S)


def runtime_process(root: Path) -> list[int]:
    """The runtime launched under ``root`` (a child of pytest), without its own children."""
    return [pid for pid in runtime_pids_under(root) if _parent_pid(pid) == os.getpid()]


def _stat(pid: int) -> list[str]:
    """``/proc/<pid>/stat`` fields after the command name: ``[state, ppid, ...]``."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except OSError:
        return []


def _parent_pid(pid: int) -> int | None:
    fields = _stat(pid)
    return int(fields[1]) if fields else None


def alive(pid: int) -> bool:
    fields = _stat(pid)
    return bool(fields) and fields[0] != "Z"


def descendants(root_pid: int) -> dict[int, str]:
    """Live descendants of ``root_pid`` with their command lines.

    Tool processes do not inherit DSH_HOME, so ``runtime_pids_under`` cannot see them.
    """
    parents = {int(e.name): _parent_pid(int(e.name)) for e in Path("/proc").glob("[0-9]*")}
    found: set[int] = set()
    frontier = {root_pid}
    while frontier:
        frontier = {pid for pid, parent in parents.items() if parent in frontier} - found
        found |= frontier
    return {pid: _cmdline(pid) for pid in found if alive(pid)}


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except OSError:
        return ""


def dialogue(body: JsonObject) -> list[tuple[str, str]]:
    """The non-system ``(role, text)`` messages of one model request."""
    return [(role, text) for role, text in conversation(body) if role != "system"]


def ignore(_event: UiEvent) -> None:
    return


def slow_reply() -> Reply:
    """A reply that streams for ~10 s, so a turn is reliably in flight."""
    return text_reply("slow " * 40, chunks=40, chunk_delay_s=0.25)


class TurnThread:
    """Runs ``bridge.send`` on its own thread, like the app's worker."""

    def __init__(
        self, bridge: AgentBridge, text: str, on_event: Callable[[UiEvent], None] = ignore
    ) -> None:
        self._outcome: TurnOutcome | None = None
        self._error: Exception | None = None
        self._thread = threading.Thread(
            target=self._run, args=(bridge, text, on_event), daemon=True
        )
        self._thread.start()

    def _run(self, bridge: AgentBridge, text: str, on_event: Callable[[UiEvent], None]) -> None:
        try:
            self._outcome = bridge.send(text, on_event)
        except Exception as error:
            self._error = error

    def result(self, timeout: float = WAIT_S) -> TurnOutcome:
        self._thread.join(timeout)
        assert not self._thread.is_alive(), "send() did not return"
        if self._error is not None:
            raise self._error
        assert self._outcome is not None
        return self._outcome


def race(*calls: Callable[[], object]) -> list[object]:
    """Release every call at the same moment, each on its own thread; its result or error."""
    barrier = threading.Barrier(len(calls))
    results: list[object] = [None] * len(calls)

    def run(index: int, call: Callable[[], object]) -> None:
        barrier.wait()
        try:
            results[index] = call()
        except Exception as error:
            results[index] = error

    threads = [threading.Thread(target=run, args=item, daemon=True) for item in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT_S)
    assert not any(thread.is_alive() for thread in threads), "a racing call did not return"
    return results


class Recorder:
    """Thread-safe ``on_event`` sink."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[UiEvent] = []

    def __call__(self, event: UiEvent) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> list[UiEvent]:
        with self._lock:
            return list(self._events)

    def of(self, kind: type) -> list[UiEvent]:
        return [event for event in self.events if isinstance(event, kind)]


@pytest.fixture
def make_bridge(make_harness_config: HarnessConfigFactory) -> Iterator[BridgeFactory]:
    """Build bridges over isolated configs; every bridge is closed at teardown."""
    bridges: list[AgentBridge] = []

    def factory(profile: str = "sdk-minimal", **overrides: object) -> AgentBridge:
        bridge = AgentBridge(make_harness_config(profile, **overrides))
        bridges.append(bridge)
        return bridge

    yield factory
    for bridge in bridges:
        bridge.close()


def test_start_is_idempotent_and_close_reaps_the_runtime(
    make_bridge: BridgeFactory, tmp_path: Path
) -> None:
    bridge = make_bridge()

    bridge.start()
    first = runtime_process(tmp_path)
    bridge.start()

    assert len(first) == 1
    assert runtime_process(tmp_path) == first
    bridge.close()
    wait_for(lambda: not runtime_pids_under(tmp_path))


@pytest.mark.parametrize("attempt", range(3))
def test_start_racing_the_first_send_boots_exactly_one_runtime(
    attempt: int, make_bridge: BridgeFactory, fake: FakeDeepSeek, tmp_path: Path
) -> None:
    """The app's boot worker calls start() while a prompt sent during "starting…" calls send()."""
    fake.enqueue(text_reply("hi"))
    bridge = make_bridge()

    started, sent = race(bridge.start, lambda: bridge.send("hello", ignore))

    assert (started, sent) == (None, TurnOutcome("completed"))
    assert len(runtime_process(tmp_path)) == 1  # a second, orphaned runtime would outlive dstui
    bridge.close()
    wait_for(lambda: not runtime_pids_under(tmp_path))


@pytest.mark.parametrize("effort", EFFORTS)
def test_the_runtime_accepts_every_advertised_reasoning_effort(
    effort: str, make_bridge: BridgeFactory, tmp_path: Path
) -> None:
    bridge = make_bridge(reasoning_effort=effort)

    bridge.start()  # initialize rejects an unknown effort (see the "bogus" test below)

    assert len(runtime_process(tmp_path)) == 1
    bridge.close()
    wait_for(lambda: not runtime_pids_under(tmp_path))


def test_send_without_start_delivers_reply_events_and_completes(
    make_bridge: BridgeFactory, fake: FakeDeepSeek
) -> None:
    fake.enqueue(text_reply("Hello **there**.", reasoning="the user greets me"))
    bridge = make_bridge()
    recorder = Recorder()

    outcome = bridge.send("hello", recorder)

    assert outcome == TurnOutcome("completed")
    assert recorder.of(Reasoning) == [Reasoning("the user greets me")]
    assert recorder.of(AssistantText) == [AssistantText("Hello **there**.")]
    assert recorder.of(TurnFinished) == [TurnFinished("completed")]
    kinds = [type(event) for event in recorder.events]
    assert kinds.index(Reasoning) < kinds.index(AssistantText) < kinds.index(TurnFinished)


def test_history_is_kept_across_turns(make_bridge: BridgeFactory, fake: FakeDeepSeek) -> None:
    fake.enqueue(text_reply("one"), text_reply("two"))
    bridge = make_bridge()

    bridge.send("first", ignore)
    outcome = bridge.send("second", ignore)

    assert outcome == TurnOutcome("completed")
    assert dialogue(fake.requests[1]) == [
        ("user", "first"),
        ("assistant", "one"),
        ("user", "second"),
    ]


def test_tool_turn_reports_the_call_and_its_real_output(
    make_bridge: BridgeFactory, fake: FakeDeepSeek
) -> None:
    args = {"command": "echo tool-output-marker"}
    fake.enqueue(tool_call_reply("bash", args, call_id="call_echo"), text_reply("done"))
    bridge = make_bridge()
    recorder = Recorder()

    outcome = bridge.send("run it", recorder)

    assert outcome == TurnOutcome("completed")
    tool_events = [e for e in recorder.events if isinstance(e, ToolCallStarted | ToolFinished)]
    assert [type(e) for e in tool_events] == [ToolCallStarted, ToolFinished]
    started, finished = tool_events
    assert isinstance(started, ToolCallStarted) and isinstance(finished, ToolFinished)
    assert (started.call_id, started.name, json.loads(started.arguments)) == (
        "call_echo",
        "bash",
        args,
    )
    assert (finished.call_id, finished.is_error) == ("call_echo", False)
    assert finished.output.startswith("tool-output-marker\n")
    assert recorder.of(AssistantText) == [AssistantText("done")]


def test_on_event_errors_are_swallowed_and_later_events_still_arrive(
    make_bridge: BridgeFactory, fake: FakeDeepSeek, caplog: pytest.LogCaptureFixture
) -> None:
    fake.enqueue(text_reply("still here", reasoning="thinking"))
    bridge = make_bridge()
    delivered: list[UiEvent] = []

    def explode(event: UiEvent) -> None:
        delivered.append(event)
        raise RuntimeError("the UI is broken")

    outcome = bridge.send("hello", explode)

    assert outcome == TurnOutcome("completed")
    # Reasoning and AssistantText come from ONE notification: the text still arrives
    assert delivered.index(Reasoning("thinking")) < delivered.index(AssistantText("still here"))
    assert delivered[-2:] == [TurnFinished("completed"), StatusChanged("idle")]
    assert "the UI is broken" in caplog.text


def test_mapping_errors_are_swallowed(
    make_bridge: BridgeFactory, fake: FakeDeepSeek, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.enqueue(text_reply("mapped anyway"))
    bridge = make_bridge()
    recorder = Recorder()

    def flaky_map(notification: Notification, root_session_id: str) -> list[UiEvent]:
        if notification.method == "session.status":
            raise ValueError("mapper bug")
        return map_notification(notification, root_session_id)

    monkeypatch.setattr(dstui.bridge, "map_notification", flaky_map)

    outcome = bridge.send("hello", recorder)

    assert outcome == TurnOutcome("completed")
    assert recorder.of(AssistantText) == [AssistantText("mapped anyway")]


@pytest.mark.parametrize(
    ("reply", "code"),
    [(auth_error_reply(), "AUTH"), (error_reply(500, "fake outage"), "SERVER")],
    ids=["auth-401", "server-500"],
)
def test_provider_error_ends_the_turn_and_the_session_recovers(
    make_bridge: BridgeFactory, fake: FakeDeepSeek, reply: Reply, code: str
) -> None:
    fake.enqueue(reply, text_reply("ok now"))
    bridge = make_bridge()
    recorder = Recorder()

    failed = bridge.send("hello", recorder)
    recovered = bridge.send("again", ignore)

    assert failed == TurnOutcome("error")
    [finished] = recorder.of(TurnFinished)
    assert isinstance(finished, TurnFinished)
    assert (finished.kind, finished.code) == ("error", code)
    assert finished.message
    assert recovered == TurnOutcome("completed")
    users = [text for role, text in dialogue(fake.requests[-1]) if role == "user"]
    assert users in (["hello", "again"], ["helloagain"])  # 0.1.7 merges user messages


def test_truncated_reply_finishes_with_max_tokens(
    make_bridge: BridgeFactory, fake: FakeDeepSeek
) -> None:
    fake.enqueue(text_reply("partial answ", finish="length"))
    bridge = make_bridge()
    recorder = Recorder()

    outcome = bridge.send("write a novel", recorder)

    assert outcome == TurnOutcome("max-tokens")
    assert recorder.of(AssistantText) == [AssistantText("partial answ")]
    assert recorder.of(TurnFinished) == [TurnFinished("max-tokens")]


def test_cancel_stops_a_running_turn_and_the_next_send_starts_fresh(
    make_bridge: BridgeFactory, fake: FakeDeepSeek, tmp_path: Path
) -> None:
    fake.enqueue(slow_reply(), text_reply("fresh"))
    bridge = make_bridge()
    turn = TurnThread(bridge, "long task")
    wait_for(lambda: len(fake.recorded) >= 1)  # the model request is in flight
    old_runtime = set(runtime_pids_under(tmp_path))

    started = time.monotonic()
    bridge.cancel()
    outcome = turn.result()
    took = time.monotonic() - started

    assert outcome == TurnOutcome(None, cancelled=True)
    assert took < STOP_S
    wait_for(lambda: not old_runtime & set(runtime_pids_under(tmp_path)))
    assert bridge.send("next", ignore) == TurnOutcome("completed")
    assert dialogue(fake.requests[-1]) == [("user", "next")]  # context was lost


def test_cancel_when_idle_keeps_the_runtime_and_the_conversation(
    make_bridge: BridgeFactory, fake: FakeDeepSeek, tmp_path: Path
) -> None:
    fake.enqueue(text_reply("one"), text_reply("two"))
    bridge = make_bridge()
    bridge.cancel()  # before anything was started
    bridge.send("first", ignore)
    runtime = runtime_process(tmp_path)

    bridge.cancel()  # between turns (e.g. Stop pressed just as the turn ended)
    outcome = bridge.send("second", ignore)

    assert outcome == TurnOutcome("completed")
    assert runtime_process(tmp_path) == runtime
    assert dialogue(fake.requests[-1]) == [
        ("user", "first"),
        ("assistant", "one"),
        ("user", "second"),
    ]


def test_close_during_a_turn_returns_cancelled_and_reaps_the_runtime(
    make_bridge: BridgeFactory, fake: FakeDeepSeek, tmp_path: Path
) -> None:
    fake.enqueue(slow_reply())
    bridge = make_bridge()
    turn = TurnThread(bridge, "long task")
    wait_for(lambda: len(fake.recorded) >= 1)

    started = time.monotonic()
    bridge.close()
    outcome = turn.result()

    assert outcome == TurnOutcome(None, cancelled=True)
    assert time.monotonic() - started < STOP_S
    wait_for(lambda: not runtime_pids_under(tmp_path))


def test_close_is_idempotent_even_before_start(make_bridge: BridgeFactory) -> None:
    bridge = make_bridge()

    bridge.close()
    bridge.close()


def test_closed_bridge_never_boots_a_runtime_again(
    make_bridge: BridgeFactory, fake: FakeDeepSeek, tmp_path: Path
) -> None:
    fake.enqueue(text_reply("too late"))
    bridge = make_bridge()
    bridge.start()
    bridge.close()
    bridge.close()

    outcome = bridge.send("hello", ignore)  # e.g. a worker racing the app's quit

    assert outcome == TurnOutcome(None, cancelled=True)
    with pytest.raises(TransportClosedError):
        bridge.start()
    assert fake.recorded == []
    assert runtime_pids_under(tmp_path) == []


def test_runtime_crash_mid_turn_raises_and_the_next_send_recovers(
    make_bridge: BridgeFactory, fake: FakeDeepSeek, tmp_path: Path
) -> None:
    fake.enqueue(slow_reply(), text_reply("after the crash"))
    bridge = make_bridge()
    turn = TurnThread(bridge, "doomed")
    wait_for(lambda: len(fake.recorded) >= 1)
    crashed = runtime_process(tmp_path)
    assert crashed

    for pid in crashed:
        os.kill(pid, signal.SIGKILL)
    with pytest.raises(TransportClosedError):
        turn.result()
    recorder = Recorder()
    outcome = bridge.send("hello again", recorder)

    assert outcome == TurnOutcome("completed")
    assert recorder.of(AssistantText) == [AssistantText("after the crash")]
    assert not set(crashed) & set(runtime_pids_under(tmp_path))
    assert dialogue(fake.requests[-1]) == [("user", "hello again")]


def test_an_error_other_than_a_dead_runtime_keeps_the_runtime_and_the_conversation(
    make_bridge: BridgeFactory,
    fake: FakeDeepSeek,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake.enqueue(text_reply("one"), text_reply("two"))
    bridge = make_bridge()
    bridge.send("first", ignore)
    runtime = runtime_process(tmp_path)

    def reject(session: Session, *args: Any, **kwargs: Any) -> RunResult:
        raise JsonRpcError(-32000, "prompt rejected")

    monkeypatch.setattr(Session, "run", reject)
    with pytest.raises(JsonRpcError, match="prompt rejected"):
        bridge.send("rejected", ignore)
    monkeypatch.undo()
    outcome = bridge.send("second", ignore)

    assert outcome == TurnOutcome("completed")
    assert runtime_process(tmp_path) == runtime  # same runtime, not a restart
    assert dialogue(fake.requests[-1]) == [
        ("user", "first"),
        ("assistant", "one"),
        ("user", "second"),
    ]


def test_new_conversation_uses_a_fresh_session_on_the_same_runtime(
    make_bridge: BridgeFactory, fake: FakeDeepSeek, tmp_path: Path
) -> None:
    fake.enqueue(text_reply("one"), text_reply("two"))
    bridge = make_bridge()
    bridge.send("first", ignore)
    runtime = runtime_process(tmp_path)

    bridge.new_conversation()
    outcome = bridge.send("second", ignore)

    assert outcome == TurnOutcome("completed")
    assert dialogue(fake.requests[-1]) == [("user", "second")]
    assert runtime_process(tmp_path) == runtime  # no restart needed


def test_cancel_racing_the_end_of_run_reports_cancelled(
    make_bridge: BridgeFactory, fake: FakeDeepSeek, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop lands after run() returned but before send() did: the context is gone, say so."""
    fake.enqueue(text_reply("one"), text_reply("two"))
    bridge = make_bridge()
    real_run = Session.run

    def run_then_cancel(session: Session, *args: Any, **kwargs: Any) -> RunResult:
        result = real_run(session, *args, **kwargs)
        bridge.cancel()
        return result

    monkeypatch.setattr(Session, "run", run_then_cancel)
    outcome = bridge.send("first", ignore)
    monkeypatch.undo()

    assert outcome == TurnOutcome(None, cancelled=True)
    assert bridge.send("second", ignore) == TurnOutcome("completed")
    assert dialogue(fake.requests[-1]) == [("user", "second")]


def test_cancel_racing_the_start_of_run_reports_cancelled(
    make_bridge: BridgeFactory, fake: FakeDeepSeek, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop lands after send() opened its session but before run() sent the prompt."""
    fake.enqueue(text_reply("fresh"))
    bridge = make_bridge()
    real_run = Session.run

    def cancel_then_run(session: Session, *args: Any, **kwargs: Any) -> RunResult:
        bridge.cancel()
        return real_run(session, *args, **kwargs)

    monkeypatch.setattr(Session, "run", cancel_then_run)
    outcome = bridge.send("never sent", ignore)
    monkeypatch.undo()

    assert outcome == TurnOutcome(None, cancelled=True)
    assert fake.recorded == []
    assert bridge.send("next", ignore) == TurnOutcome("completed")
    assert dialogue(fake.requests[-1]) == [("user", "next")]


def test_invalid_reasoning_effort_fails_start_without_a_stuck_state(
    make_harness_config: HarnessConfigFactory, fake: FakeDeepSeek, tmp_path: Path
) -> None:
    config = make_harness_config(reasoning_effort="bogus")
    bridge = AgentBridge(config)
    try:
        with pytest.raises(JsonRpcError, match=r"(?i)reasoning"):
            bridge.start()
        with pytest.raises(JsonRpcError):  # retried from scratch, not stuck half-open
            bridge.send("hello", ignore)
        assert runtime_pids_under(tmp_path) == []

        config.reasoning_effort = None  # the SDK re-reads its config on every start
        fake.enqueue(text_reply("works now"))
        bridge.start()
        outcome = bridge.send("hello", ignore)
    finally:
        bridge.close()

    assert outcome == TurnOutcome("completed")


def test_default_sdk_profile_completes_a_text_turn(
    make_bridge: BridgeFactory, fake: FakeDeepSeek
) -> None:
    fake.enqueue(text_reply("hello from the sdk profile", reasoning="short"))
    bridge = make_bridge("sdk")
    recorder = Recorder()

    outcome = bridge.send("hi", recorder)

    assert outcome == TurnOutcome("completed")
    assert recorder.of(AssistantText) == [AssistantText("hello from the sdk profile")]
    assert recorder.of(Reasoning) == [Reasoning("short")]
    assert "edit" in tool_names(fake.requests[0])  # sdk-minimal offers only "bash"


def test_cancel_while_a_tool_runs_leaves_no_tool_processes(
    make_bridge: BridgeFactory, fake: FakeDeepSeek, tmp_path: Path
) -> None:
    fake.enqueue(tool_call_reply("bash", {"command": "sleep 30"}), text_reply("never"))
    bridge = make_bridge()
    tool_started = threading.Event()

    def on_event(event: UiEvent) -> None:
        if isinstance(event, ToolCallStarted):
            tool_started.set()

    turn = TurnThread(bridge, "wait a while", on_event)
    assert tool_started.wait(WAIT_S)
    [runtime] = runtime_process(tmp_path)
    wait_for(lambda: "sleep 30" in " ".join(descendants(runtime).values()))
    processes = {runtime, *descendants(runtime)}  # runtime, its shell and the sleep
    assert len(processes) >= 3 and all(alive(pid) for pid in processes)

    started = time.monotonic()
    bridge.cancel()
    outcome = turn.result()

    assert outcome == TurnOutcome(None, cancelled=True)
    assert time.monotonic() - started < STOP_S  # ~1.06 s: shutdown times out on the busy shell
    wait_for(lambda: not any(alive(pid) for pid in processes), timeout=STOP_S)


def test_runtime_death_between_turns_fails_one_send_then_recovers(
    make_bridge: BridgeFactory, fake: FakeDeepSeek, tmp_path: Path
) -> None:
    fake.enqueue(text_reply("one"), text_reply("two"))
    bridge = make_bridge()
    bridge.send("first", ignore)
    for pid in runtime_process(tmp_path):
        os.kill(pid, signal.SIGKILL)
    wait_for(lambda: not runtime_pids_under(tmp_path))

    with pytest.raises(TransportClosedError):  # the conversation is gone: say so once
        bridge.send("lost", ignore)
    outcome = bridge.send("second", ignore)

    assert outcome == TurnOutcome("completed")
    assert dialogue(fake.requests[-1]) == [("user", "second")]
