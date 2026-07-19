"""Retry with exponential backoff and full jitter.

Upstream feeds are flaky in boring ways — a 502 from a CDN, a connection reset,
a rate-limit burst — and a nightly job that gives up on the first blip is worse
than useless. The delay before attempt *n* is::

    random.uniform(0, min(max_backoff, backoff * 2 ** (n - 1)))

"Full jitter" (uniform over the whole window rather than base + jitter) is used
because several sources in the same pipeline otherwise retry in lockstep and
re-create the burst that caused the failure.

``sleep`` and ``rng`` are injectable so tests can assert the delay sequence
without spending real time.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to retry and how long to wait."""

    attempts: int = 4
    backoff: float = 0.5
    max_backoff: float = 30.0
    jitter: bool = True

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")
        if self.backoff < 0:
            raise ValueError("backoff must not be negative")

    @classmethod
    def from_retries(
        cls, retries: int, backoff: float = 0.5, max_backoff: float = 30.0
    ) -> "RetryPolicy":
        """Build a policy from a *retry* count (total attempts = retries + 1)."""
        return cls(attempts=max(1, retries + 1), backoff=backoff, max_backoff=max_backoff)

    def delay_for(self, attempt: int, rng: random.Random | None = None) -> float:
        """Delay in seconds before ``attempt`` (1-based; attempt 1 never waits)."""
        if attempt <= 1:
            return 0.0
        window = min(self.max_backoff, self.backoff * (2 ** (attempt - 2)))
        if not self.jitter:
            return window
        generator = rng or random
        return generator.uniform(0.0, window)

    def delays(self, rng: random.Random | None = None) -> Iterable[float]:
        """The full sequence of delays this policy would use."""
        return [self.delay_for(attempt, rng) for attempt in range(2, self.attempts + 1)]


def call_with_retries(
    operation: Callable[[int], T],
    policy: RetryPolicy,
    *,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    should_retry: Callable[[BaseException], bool] | None = None,
    on_retry: Callable[[int, float, BaseException], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> T:
    """Call ``operation(attempt)`` until it succeeds or the budget runs out.

    Args:
        operation: Receives the 1-based attempt number.
        policy: Attempt count and backoff shape.
        retry_on: Exception types considered retryable.
        should_retry: Extra predicate; return ``False`` to fail immediately
            (used to avoid retrying a 404, which will never become a 200).
        on_retry: Called with ``(next_attempt, delay, error)`` before sleeping.
        sleep: Injectable sleep function.
        rng: Injectable random source.

    Returns:
        Whatever ``operation`` returned.

    Raises:
        BaseException: The last error, once every attempt is exhausted.
    """
    last_error: BaseException | None = None
    for attempt in range(1, policy.attempts + 1):
        try:
            return operation(attempt)
        except retry_on as error:  # noqa: PERF203 - retry loop is the point
            last_error = error
            if should_retry is not None and not should_retry(error):
                raise
            if attempt >= policy.attempts:
                raise
            delay = policy.delay_for(attempt + 1, rng)
            if on_retry is not None:
                on_retry(attempt + 1, delay, error)
            if delay > 0:
                sleep(delay)
    raise last_error  # type: ignore[misc]  # pragma: no cover - unreachable
