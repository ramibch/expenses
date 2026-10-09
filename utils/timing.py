"""Per-request timing instrumentation.

A :class:`~contextvars.ContextVar` holds the active :class:`RequestTimer`, so any
layer in the call stack (handlers, the Telegram client, the AI client) can record
a step with :func:`timed` without threading a timer through every function.

This is safe under threads (Huey workers) and async code: each request gets its
own context, so concurrent requests never share a timer.

When no timer is active, :func:`timed` is a no-op, which keeps production paths
unaffected if instrumentation is turned off.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator


class RequestTimer:
    """Collects ``(step name, duration in ms)`` pairs for one request."""

    def __init__(self) -> None:
        self._start = time.perf_counter()
        self.steps: list[tuple[str, float]] = []

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        """Time the enclosed block and append it to :attr:`steps`."""
        start = time.perf_counter()
        try:
            yield
        finally:
            self.steps.append((name, (time.perf_counter() - start) * 1000.0))

    def total_ms(self) -> float:
        """Wall-clock time since the timer was created, in milliseconds."""
        return (time.perf_counter() - self._start) * 1000.0


_active: ContextVar[RequestTimer | None] = ContextVar("request_timer", default=None)


def start_timer() -> tuple[RequestTimer, "Token[RequestTimer | None]"]:
    """Create a timer and make it the active one for the current context."""
    timer = RequestTimer()
    return timer, _active.set(timer)


def stop_timer(token: "Token[RequestTimer | None]") -> None:
    """Restore the previous timer (call in a ``finally`` next to ``start_timer``)."""
    _active.reset(token)


def active_timer() -> RequestTimer | None:
    """Return the active timer for the current context, if any."""
    return _active.get()


@contextmanager
def timed(name: str) -> Iterator[None]:
    """Record a step called ``name`` on the active timer, or do nothing."""
    timer = _active.get()
    if timer is None:
        yield
        return
    with timer.step(name):
        yield
