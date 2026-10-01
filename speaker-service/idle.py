"""Idle watchdog for the on-demand speaker-service.

The HTTP service is started by ``vs-speaker-up.sh`` and exits on its own after
``VASTACK_IDLE_EXIT_S`` seconds (default 600) of idleness. ``idle`` means ALL
THREE conditions hold:

1. no in-flight HTTP request (tracked by the app middleware),
2. no ``jobs`` row in ``queued``/``running`` (the durable registry is the
   source of truth, so a long transcription defers the exit), and
3. no ``/health`` keepalive within the window (the Web UI polls it every 30 s,
   so an open UI keeps the service alive; the launcher's readiness probe counts
   as the first keepalive).

The window is measured from the later of the last ``/health`` request and the
last poll that observed an in-flight request or an active job, so a
just-finished job still gets the full window before the service may exit.

``VASTACK_IDLE_EXIT_S=0`` (or negative) disables the watchdog - the opt-in
always-on launchd variant relies on this so ``KeepAlive`` has a stable process.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger("uvicorn.error")

DEFAULT_CHECK_INTERVAL_S = 1.0

Sleep = Callable[[float], Awaitable[None]]


class ActivityTracker:
    """In-flight HTTP request count plus the last ``/health`` keepalive."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._in_flight = 0
        self._last_keepalive_at = clock()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    @property
    def last_keepalive_at(self) -> float:
        return self._last_keepalive_at

    def request_started(self) -> None:
        self._in_flight += 1

    def request_finished(self, path: str) -> None:
        self._in_flight -= 1
        if path == "/health":
            self._last_keepalive_at = self._clock()


class IdleWatchdog:
    """Background task that requests shutdown once the idle window elapses."""

    def __init__(
        self,
        *,
        tracker: ActivityTracker,
        jobs_active: Callable[[], bool],
        idle_after_s: float,
        on_idle: Callable[[], None],
        check_interval_s: float = DEFAULT_CHECK_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._tracker = tracker
        self._jobs_active = jobs_active
        self._idle_after_s = idle_after_s
        self._on_idle = on_idle
        self._check_interval_s = check_interval_s
        self._clock = clock
        self._sleep = sleep
        self._last_busy_at = clock()

    @property
    def idle_after_s(self) -> float:
        return self._idle_after_s

    def idle_for(self, now: float) -> float | None:
        """Seconds since the last activity, or ``None`` while busy.

        Busy means an in-flight request or a queued/running job; observing
        either refreshes the window so the full timeout follows the last busy
        poll.
        """
        if self._tracker.in_flight > 0 or self._jobs_active():
            self._last_busy_at = now
            return None
        return now - max(self._last_busy_at, self._tracker.last_keepalive_at)

    async def run(self) -> None:
        """Poll until idle for the whole window, then invoke ``on_idle`` once."""
        if self._idle_after_s <= 0:
            return
        while True:
            await self._sleep(self._check_interval_s)
            idle = self.idle_for(self._clock())
            if idle is not None and idle >= self._idle_after_s:
                logger.info(
                    "idle watchdog: %.0fs without keepalive or job; shutting down",
                    idle,
                )
                self._on_idle()
                return


def terminate_process() -> None:
    """Ask uvicorn for a graceful shutdown (SIGTERM is its documented signal)."""
    signal.raise_signal(signal.SIGTERM)
