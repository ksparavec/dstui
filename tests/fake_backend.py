"""A scriptable, thread-safe ``AgentBackend`` for fast, deterministic UI tests (no runtime).

Script one turn per ``send`` with ``script(*steps)``. A step is either a UI event, which is
passed to ``on_event``, or a ``threading.Event`` gate, which blocks the turn until the test
sets it (or until ``cancel``/``close`` interrupts the turn, which then returns cancelled).
``gates={"start": event}`` blocks a method until the event is set (or ``close`` is called);
``fail={"cancel": error}`` makes a method raise. Every call is recorded with the id of the
thread it ran on.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from dstui.bridge import TurnOutcome
from dstui.events import UiEvent

GATE_TIMEOUT_S = 10.0  # a gate the test forgot to open fails the turn instead of hanging
_POLL_S = 0.005

type Step = UiEvent | threading.Event


@dataclass(frozen=True, slots=True)
class Call:
    name: str
    thread_id: int
    args: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True)
class ScriptedTurn:
    steps: tuple[Step, ...] = ()
    outcome: TurnOutcome = field(default_factory=lambda: TurnOutcome("completed"))
    error: BaseException | None = None


class FakeBackend:
    """Implements ``dstui.bridge.AgentBackend``; unscripted turns complete with no events."""

    def __init__(
        self,
        *,
        gates: Mapping[str, threading.Event] | None = None,
        fail: Mapping[str, BaseException] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._turns: deque[ScriptedTurn] = deque()
        self._calls: list[Call] = []
        self._callbacks: list[Callable[[UiEvent], None]] = []
        self._interrupted = threading.Event()
        self.gates = dict(gates or {})
        self.fail = dict(fail or {})
        self.blocked = threading.Event()  # set while a turn waits on one of its gates
        self.closed = threading.Event()

    # -- scripting and inspection (test side) -------------------------------------------------

    def script(
        self,
        *steps: Step,
        outcome: TurnOutcome | None = None,
        error: BaseException | None = None,
    ) -> None:
        """Queue the next turn: emit ``steps`` in order, then return ``outcome`` or raise."""
        turn = ScriptedTurn(steps, outcome or TurnOutcome("completed"), error)
        with self._lock:
            self._turns.append(turn)

    @property
    def calls(self) -> list[Call]:
        with self._lock:
            return list(self._calls)

    def calls_named(self, name: str) -> list[Call]:
        return [call for call in self.calls if call.name == name]

    @property
    def sent(self) -> list[str]:
        return [str(call.args[0]) for call in self.calls_named("send")]

    @property
    def interrupt_pending(self) -> bool:
        """A cancel()/close() is set to interrupt the current (or next) gate of a turn."""
        return self._interrupted.is_set()

    @property
    def callbacks(self) -> list[Callable[[UiEvent], None]]:
        """The ``on_event`` callback of every ``send`` so far (to replay stale events)."""
        with self._lock:
            return list(self._callbacks)

    # -- AgentBackend -------------------------------------------------------------------------

    def start(self) -> None:
        self._enter("start")

    def send(self, text: str, on_event: Callable[[UiEvent], None]) -> TurnOutcome:
        self._enter("send", text)
        with self._lock:
            turn = self._turns.popleft() if self._turns else ScriptedTurn()
            self._callbacks.append(on_event)
            if not self.closed.is_set():
                self._interrupted.clear()
        for step in turn.steps:
            if not isinstance(step, threading.Event):
                on_event(step)
            elif not self._wait_in_turn(step):
                return TurnOutcome(None, cancelled=True)
        if turn.error is not None:
            raise turn.error
        return turn.outcome

    def cancel(self) -> None:
        self._enter("cancel")
        self._interrupted.set()

    def new_conversation(self) -> None:
        self._enter("new_conversation")

    def close(self) -> None:
        self._record("close")
        self.closed.set()  # always unblock everything first, like the real bridge
        self._interrupted.set()
        if (error := self.fail.get("close")) is not None:
            raise error

    # -- internals ----------------------------------------------------------------------------

    def _record(self, name: str, *args: object) -> None:
        with self._lock:
            self._calls.append(Call(name, threading.get_ident(), args))

    def _enter(self, name: str, *args: object) -> None:
        self._record(name, *args)
        if (gate := self.gates.get(name)) is not None:
            _wait(gate, self.closed)
        if (error := self.fail.get(name)) is not None:
            raise error

    def _wait_in_turn(self, gate: threading.Event) -> bool:
        self.blocked.set()
        try:
            return _wait(gate, self._interrupted)
        finally:
            self.blocked.clear()


def _wait(gate: threading.Event, interrupt: threading.Event) -> bool:
    """Block until ``gate`` opens (True) or ``interrupt`` is set (False)."""
    deadline = time.monotonic() + GATE_TIMEOUT_S
    while not gate.wait(_POLL_S):
        if interrupt.is_set():
            return False
        if time.monotonic() > deadline:
            raise TimeoutError("FakeBackend gate was never opened")
    return True
