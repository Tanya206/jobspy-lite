"""Process-wide, thread-safe rate limiter shared across all AI API calls."""

import threading
import time
from collections import deque


class RateLimiter:
    """Sliding-window rate limiter. Blocks the calling thread until a slot
    is free rather than rejecting — appropriate for a bounded worker pool
    where callers are OK waiting their turn."""

    def __init__(self, max_calls: int, period: float = 60.0):
        self.max_calls = max_calls
        self.period = period
        self._calls: deque = deque()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)

    def recent_count(self) -> int:
        """Calls made inside the current sliding window."""
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= self.period:
                self._calls.popleft()
            return len(self._calls)

    @property
    def max_calls(self) -> int:
        return self._max_calls

    @max_calls.setter
    def max_calls(self, value: int) -> None:
        self._max_calls = max(1, int(value))

    def acquire(self) -> float:
        """Block until a slot is free. Returns seconds waited (0 if immediate)."""
        start = time.monotonic()
        with self._cv:
            while True:
                now = time.monotonic()
                # Drop timestamps that have aged out of the window.
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()

                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    waited = time.monotonic() - start
                    try:
                        from .observability import AI_METRICS

                        AI_METRICS.record_rate_wait(waited)
                    except Exception:  # noqa: BLE001 - metrics must not block a call
                        pass
                    return waited

                wait_for = self.period - (now - self._calls[0])
                # Wait, then re-check (another thread may have raced us).
                self._cv.wait(timeout=max(wait_for, 0.01))


# Default rpm before any config (env or dashboard) is applied; kept as a
# named constant so configure_rate_limit can tell "untouched" from "already
# overridden at runtime".
DEFAULT_RATE_LIMIT = 20

# Singleton instance — import this everywhere, never construct a new one.
# Default 30 rpm gives headroom under NVIDIA's 40 rpm cap even if other
# processes or a burst share the same key. AI_RATE_LIMIT_PER_MIN overrides it
# (ai_client.configure_rate_limit applies the env value at client setup).
NVIDIA_RATE_LIMITER = RateLimiter(max_calls=DEFAULT_RATE_LIMIT, period=60.0)
