"""Process-wide, thread-safe metrics for the AI scoring agents.

Everything the agents do during a run is counted here so it can be surfaced
in the dashboard, the run history and the logs:

- ``calls`` / ``successes`` / ``failures`` — logical ``AIClient.parse()``
  invocations (one per agent step), i.e. what the pipeline asked for.
- ``attempts`` — raw HTTP attempts inside those calls; ``retries`` is
  ``attempts - calls`` (every attempt beyond the first per call).
- latency — wall time of each HTTP attempt (excludes retry backoff sleeps),
  so ``avg_latency_s`` reflects the API, not our backoff policy.
- ``errors_by_status`` — HTTP status counts of failed attempts (429/503
  bursts show up here); non-HTTP errors land under ``"other"``.
- rate-limit waits — time scoring threads spent blocked on the shared
  sliding-window limiter (``RateLimiter.acquire``).

One window per pipeline run: the pipeline calls :meth:`AI_METRICS.reset`
when a run starts and snapshots at the end; between runs the endpoint keeps
showing the last window. Only one pipeline runs at a time (``_PIPELINE_LOCK``)
so a reset-per-run window is safe.
"""

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# A wait shorter than this is lock contention, not the sliding window
# pushing back; only real limiter waits count as "rate-limited".
RATE_WAIT_THRESHOLD_S = 0.1


class AIMetrics:
    """Thread-safe counters for one observability window."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reset_locked()
        self._since_monotonic = time.monotonic()

    def _reset_locked(self) -> None:
        self.since: str = datetime.now(timezone.utc).isoformat()
        self.calls = 0
        self.successes = 0
        self.failures = 0
        self.attempts = 0
        self._latency_sum = 0.0
        self.rate_waits = 0
        self.rate_wait_s = 0.0
        self.errors_by_status: Dict[str, int] = {}
        self.last_error: Optional[str] = None

    def reset(self) -> None:
        """Start a fresh window (called at the start of each pipeline run)."""
        with self._lock:
            self._reset_locked()
            self._since_monotonic = time.monotonic()

    def record_attempt(self, latency_s: float, status: Optional[int] = None) -> None:
        """One HTTP attempt finished (success or failure)."""
        with self._lock:
            self.attempts += 1
            self._latency_sum += max(0.0, latency_s)
            if status is not None:
                key = str(status)
                self.errors_by_status[key] = self.errors_by_status.get(key, 0) + 1

    def record_error(self, status: Optional[int] = None) -> None:
        """A failed attempt: count its HTTP status (or bucket non-HTTP)."""
        with self._lock:
            key = str(status) if status is not None else "other"
            self.errors_by_status[key] = self.errors_by_status.get(key, 0) + 1

    def record_call(self, ok: bool, error: Optional[str] = None) -> None:
        """One ``parse()`` invocation finished."""
        with self._lock:
            self.calls += 1
            if ok:
                self.successes += 1
            else:
                self.failures += 1
                if error:
                    self.last_error = error[:300]

    def record_rate_wait(self, seconds: float) -> None:
        """A scoring thread spent ``seconds`` blocked on the rate limiter."""
        if seconds <= 0:
            return
        with self._lock:
            self.rate_wait_s += seconds
            if seconds >= RATE_WAIT_THRESHOLD_S:
                self.rate_waits += 1

    def snapshot(self) -> Dict[str, Any]:
        """Plain-JSON view of the current window (safe to persist/serve)."""
        with self._lock:
            avg = self._latency_sum / self.attempts if self.attempts else None
            return {
                "since": self.since,
                "window_s": round(time.monotonic() - self._since_monotonic, 1),
                "calls": self.calls,
                "successes": self.successes,
                "failures": self.failures,
                "attempts": self.attempts,
                "retries": max(0, self.attempts - self.calls),
                "avg_latency_s": round(avg, 2) if avg is not None else None,
                "rate_waits": self.rate_waits,
                "rate_wait_s": round(self.rate_wait_s, 1),
                "errors_by_status": dict(self.errors_by_status),
                "last_error": self.last_error,
            }


# Singleton — import this everywhere, never construct a new one.
AI_METRICS = AIMetrics()
