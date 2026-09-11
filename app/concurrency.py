"""Concurrency control: coalescing, admission, and a place to read both.

Two independent problems, deliberately kept separate.

**Duplicate work.** Ten concurrent requests for the same query used to run ten
searches, each leasing its own identity and each paying the full cooldown
afterwards. The cache only helps *after* the first one finishes, so a burst of
identical requests is precisely the case it cannot catch. `SingleFlight` makes the
first caller do the work and the rest wait on its result.

**Too much work.** A burst larger than the pool simply queued on `acquire()` until
a 90-second timeout, so the client learned nothing for a minute and a half and then
got an error. `AdmissionControl` bounds how many requests are in flight and how
many may wait, and refuses the rest immediately with a `Retry-After` — a fast,
honest "not now" beats a slow, uninformative failure.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class Overloaded(RuntimeError):
    """Admission refused: in-flight and queue are both full."""

    def __init__(self, message: str, retry_after_s: float):
        super().__init__(message)
        self.retry_after_s = retry_after_s


@dataclass
class _Call:
    """One in-flight piece of work, and everyone waiting on it."""

    future: asyncio.Future
    waiters: int = 1
    started_at: float = field(default_factory=time.monotonic)


class SingleFlight:
    """Collapse concurrent calls sharing a key into one execution.

    The result — *or the exception* — is delivered to every caller. Sharing the
    failure matters as much as sharing the success: if a search was blocked, five
    waiters immediately retrying it would burn five more identities on a query
    already known to be failing.
    """

    def __init__(self) -> None:
        self._calls: dict[str, _Call] = {}
        self._lock = asyncio.Lock()
        self.coalesced = 0          # requests served off someone else's work
        self.executed = 0           # requests that actually did the work

    async def do(self, key: str, fn: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            call = self._calls.get(key)
            if call is not None:
                call.waiters += 1
                self.coalesced += 1
                existing = call.future
            else:
                loop = asyncio.get_running_loop()
                call = _Call(future=loop.create_future())
                self._calls[key] = call
                self.executed += 1
                existing = None

        if existing is not None:
            # Shield: one waiter being cancelled must not cancel the shared work
            # that the others are still depending on.
            return await asyncio.shield(existing)

        try:
            result = await fn()
        except BaseException as exc:                      # noqa: BLE001 - re-raised
            async with self._lock:
                self._calls.pop(key, None)
            if not call.future.done():
                call.future.set_exception(exc)
            # Nobody may be awaiting the future; stop asyncio warning about it.
            with contextlib.suppress(Exception):
                call.future.exception()
            raise
        else:
            async with self._lock:
                self._calls.pop(key, None)
            if not call.future.done():
                call.future.set_result(result)
            return result

    @property
    def in_flight(self) -> int:
        return len(self._calls)

    def stats(self) -> dict[str, int]:
        return {
            "in_flight": self.in_flight,
            "executed": self.executed,
            "coalesced": self.coalesced,
        }


class AdmissionControl:
    """Bound concurrent work, and refuse the rest fast.

    `max_in_flight` requests run at once; up to `max_queued` more may wait. Past
    that a caller gets `Overloaded` immediately rather than discovering after a
    long timeout that it was never going to be served.

    This is deliberately not a fair queue. Under sustained overload the right
    behaviour is to shed load, not to build a backlog that guarantees every client
    a slow answer.
    """

    def __init__(self, max_in_flight: int = 8, max_queued: int = 32, retry_after_s: float = 5.0):
        self._sem = asyncio.Semaphore(max_in_flight)
        self._max_in_flight = max_in_flight
        self._max_queued = max_queued
        self._retry_after_s = retry_after_s
        self._queued = 0
        self._in_flight = 0
        self.admitted = 0
        self.rejected = 0
        self.peak_in_flight = 0

    @contextlib.asynccontextmanager
    async def slot(self):
        # Only reject when there is genuinely nowhere to put this request: no free
        # slot AND no room to wait. Checking the queue alone rejected the very
        # first caller whenever max_queued was 0 — "no waiting allowed" is not
        # "no work allowed".
        if self._sem.locked() and self._queued >= self._max_queued:
            self.rejected += 1
            raise Overloaded(
                f"{self._in_flight} in flight, {self._queued} queued, "
                f"limit {self._max_in_flight}/{self._max_queued}",
                self._retry_after_s,
            )

        self._queued += 1
        try:
            await self._sem.acquire()
        finally:
            self._queued -= 1

        self._in_flight += 1
        self._peak()
        self.admitted += 1
        try:
            yield
        finally:
            self._in_flight -= 1
            self._sem.release()

    def _peak(self) -> None:
        if self._in_flight > self.peak_in_flight:
            self.peak_in_flight = self._in_flight

    def stats(self) -> dict[str, int | float]:
        return {
            "in_flight": self._in_flight,
            "queued": self._queued,
            "max_in_flight": self._max_in_flight,
            "max_queued": self._max_queued,
            "peak_in_flight": self.peak_in_flight,
            "admitted": self.admitted,
            "rejected": self.rejected,
        }
