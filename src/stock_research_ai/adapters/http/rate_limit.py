"""In-memory limiter for the one HTTP route that spends provider credits (POST /v1/summaries).

Single local user, single worker: one process-wide limiter, never keyed by token or
client, so there is no per-caller dictionary to grow and no credential in its state -
only monotonic timestamps. A restart resets it (acceptable for local V1).

Defaults: 10 summaries per rolling 10 minutes, at most 2 in flight. Interactive use is
one click per company page, each taking ~10-30 s at the provider, so normal browsing
stays well inside the window; a stuck client, script or double-submit loop is capped at
60 provider calls per hour instead of unbounded spend.
"""

import time
from collections import deque
from collections.abc import Callable

DEFAULT_SUMMARY_LIMIT = 10
DEFAULT_SUMMARY_WINDOW_SECONDS = 600.0
DEFAULT_SUMMARY_MAX_IN_FLIGHT = 2


class RateLimited(Exception):
    def __init__(self, message: str, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.message = message
        self.retry_after_seconds = retry_after_seconds


class SummaryRateLimiter:
    """Sliding window over accepted requests plus an in-flight cap. `acquire()` either
    records one request or raises RateLimited without recording anything; `release()`
    ends an in-flight request (accepted requests still count for the whole window)."""

    def __init__(
        self,
        limit: int = DEFAULT_SUMMARY_LIMIT,
        window_seconds: float = DEFAULT_SUMMARY_WINDOW_SECONDS,
        max_in_flight: int = DEFAULT_SUMMARY_MAX_IN_FLIGHT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1 or window_seconds <= 0 or max_in_flight < 1:
            raise ValueError("Rate limits must be positive")
        self._limit = limit
        self._window = window_seconds
        self._max_in_flight = max_in_flight
        self._clock = clock
        self._accepted: deque[float] = deque(maxlen=limit)
        self._in_flight = 0

    def acquire(self) -> None:
        now = self._clock()
        while self._accepted and now - self._accepted[0] >= self._window:
            self._accepted.popleft()
        if self._in_flight >= self._max_in_flight:
            raise RateLimited("A summary is already being generated. Try again shortly.", 5)
        if len(self._accepted) >= self._limit:
            wait = self._window - (now - self._accepted[0])
            raise RateLimited(
                "Too many summaries requested recently. Try again later.",
                max(1, int(wait + 0.999)),
            )
        self._accepted.append(now)
        self._in_flight += 1

    def release(self) -> None:
        self._in_flight = max(0, self._in_flight - 1)
