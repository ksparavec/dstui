"""Thread-side owner of the DeepSeek Harness runtime and the chat session.

Every method blocks. Call them from worker threads, never from the Textual
event loop.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig, Notification, Session
from deepseek_harness.errors import TransportClosedError

from dstui.events import UiEvent, map_notification

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    finish_reason: str | None  # last turn/end kind; None when cancelled
    cancelled: bool = False


_CANCELLED = TurnOutcome(None, cancelled=True)


class AgentBackend(Protocol):
    """What the TUI needs from an agent. ``AgentBridge`` is the real implementation."""

    def start(self) -> None:
        """Boot the runtime and open a session (idempotent)."""
        ...

    def send(self, text: str, on_event: Callable[[UiEvent], None]) -> TurnOutcome:
        """Run one turn; ``on_event`` is called on the calling thread for each UI event."""
        ...

    def cancel(self) -> None:
        """Stop the in-flight turn. The runtime restarts, so conversation context is lost."""
        ...

    def new_conversation(self) -> None:
        """Forget the current conversation; the next ``send`` starts a fresh session."""
        ...

    def close(self) -> None:
        """Shut the runtime down (idempotent)."""
        ...


class AgentBridge:
    """``AgentBackend`` over the real SDK: one runtime process, one session at a time.

    One turn at a time: callers must not overlap ``send`` calls (the runtime would merge
    them). ``cancel`` only acts while a ``send`` is in progress, so a Stop that arrives
    after the turn ended keeps the conversation. ``close`` is final: afterwards ``send``
    returns cancelled and ``start`` raises, so no runtime can outlive the app.
    """

    def __init__(self, config: DeepSeekHarnessConfig) -> None:
        self._harness = DeepSeekHarness(config)
        self._lock = threading.Lock()  # lifecycle lock; never held while a turn runs
        self._session: Session | None = None
        self._generation = 0  # bumped whenever the runtime is shut down
        self._in_flight = False  # a send() is between opening its session and returning
        self._closed = False

    def start(self) -> None:
        with self._lock:
            self._open_locked()

    def send(self, text: str, on_event: Callable[[UiEvent], None]) -> TurnOutcome:
        with self._lock:
            if self._closed:  # e.g. a worker racing the app's quit: never boot again
                return _CANCELLED
            session = self._open_locked()
            generation = self._generation
            self._in_flight = True
        try:
            result = session.run(text, on_notification=_forwarder(session.id, on_event))
        except Exception as error:
            if self._end_turn(generation, crashed=isinstance(error, TransportClosedError)):
                return _CANCELLED
            raise
        if self._end_turn(generation):  # a Stop racing the end of the turn still lost it
            return _CANCELLED
        return TurnOutcome(result.finish_reason)

    def cancel(self) -> None:
        with self._lock:
            if self._in_flight:
                self._shutdown_locked()

    def new_conversation(self) -> None:
        with self._lock:
            self._session = None  # the runtime stays up; the next send mints a new session id

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._shutdown_locked()

    def _end_turn(self, generation: int, *, crashed: bool = False) -> bool:
        """Finish a send(); True when cancel()/close() shut the runtime down meanwhile."""
        with self._lock:
            self._in_flight = False
            if self._generation != generation:
                return True
            if crashed:  # the runtime died on its own: the next send() starts a fresh one
                self._shutdown_locked()
            return False

    def _shutdown_locked(self) -> None:
        self._generation += 1
        self._session = None
        self._harness.close()  # also required after a crash: start() is a no-op until then

    def _open_locked(self) -> Session:
        if self._closed:
            raise TransportClosedError("the agent bridge is closed")
        if self._session is None:
            self._session = self._harness.start_session()  # boots the runtime if needed
        return self._session


def _forwarder(
    session_id: str, on_event: Callable[[UiEvent], None]
) -> Callable[[Notification], None]:
    """An SDK callback that never raises: an escaping error would abort ``run()`` mid-turn."""

    def forward(notification: Notification) -> None:
        try:
            events = map_notification(notification, session_id)
        except Exception:
            log.exception("cannot map notification %s", notification.method)
            return
        for event in events:
            try:
                on_event(event)
            except Exception:
                log.exception("on_event failed for %r", event)

    return forward
