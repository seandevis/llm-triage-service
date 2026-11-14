"""Three limits: how many calls may be in flight, how often they may start,
and how much they may cost.

The semaphore is held around the upstream call only -- not the cache lookup, not
response serialisation -- because a permit held during work that is not the
scarce resource is a permit nobody else can use. test_concurrency.py checks this
against the mock's peak_concurrency rather than our own counter, since only the
former observes what actually arrived.

The rate limiter is the provider's own limit, enforced on our side of the wire
so we wait instead of collecting 429s. It is taken inside the semaphore, right
before the call: a slot stamped while still queueing for a permit would let
calls stamped a window apart all leave together once permits free up. Holding a
permit while waiting for a slot costs nothing, because no other permit holder
could send anything either.

The ceiling raises before the call that would exceed SPEND_CEILING_USD. A call's
true cost is unknown until it returns, so the ledger reserves the worst case up
front and reconciles after; reserving optimistically would let a burst of
concurrent calls sail past the ceiling together, each seeing room for itself.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass

from app.config import (
    PRICE_INPUT_USD_PER_MTOK,
    PRICE_OUTPUT_USD_PER_MTOK,
    Settings,
    get_settings,
)


def cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens / 1_000_000 * PRICE_INPUT_USD_PER_MTOK
        + output_tokens / 1_000_000 * PRICE_OUTPUT_USD_PER_MTOK
    )


class SpendCeilingExceeded(RuntimeError):
    """Raised instead of making the call -- the money has not moved."""

    def __init__(self, message: str, *, spent_usd: float, ceiling_usd: float) -> None:
        super().__init__(message)
        self.spent_usd = spent_usd
        self.ceiling_usd = ceiling_usd


class SpendLedger:
    """Tracks spend against a ceiling, reserving before each call.

    Reserve -> call -> settle. The reservation is the worst case the call could
    cost; settling replaces it with what it actually cost. A call that never
    happens releases its reservation in full.
    """

    def __init__(self, ceiling_usd: float, *, max_tokens: int, estimated_input_tokens: int = 1000):
        self.ceiling_usd = ceiling_usd
        self.max_tokens = max_tokens
        self.estimated_input_tokens = estimated_input_tokens
        self._settled_usd = 0.0
        self._reserved_usd = 0.0
        self._lock = asyncio.Lock()

    @property
    def spent_usd(self) -> float:
        return self._settled_usd

    @property
    def committed_usd(self) -> float:
        return self._settled_usd + self._reserved_usd

    def worst_case_usd(self) -> float:
        """The most one call could cost: max_tokens out, an estimate in."""
        return cost_usd(self.estimated_input_tokens, self.max_tokens)

    async def reserve(self) -> float:
        """Reserve worst-case budget for one call, or raise before spending it.

        Raising here -- before the request is sent -- is the whole point. The
        check is against `committed_usd` so that concurrent calls cannot each
        see room for themselves and collectively blow past the ceiling.
        """
        amount = self.worst_case_usd()
        async with self._lock:
            if self._settled_usd + self._reserved_usd + amount > self.ceiling_usd:
                raise SpendCeilingExceeded(
                    f"this call would cost up to ${amount:.6f}, taking committed spend past "
                    f"the ${self.ceiling_usd:.6f} ceiling "
                    f"(settled ${self._settled_usd:.6f}, reserved ${self._reserved_usd:.6f}); "
                    "refusing to make it",
                    spent_usd=self._settled_usd,
                    ceiling_usd=self.ceiling_usd,
                )
            self._reserved_usd += amount
            return amount

    async def settle(self, reserved: float, input_tokens: int, output_tokens: int) -> float:
        actual = cost_usd(input_tokens, output_tokens)
        async with self._lock:
            self._reserved_usd = max(0.0, self._reserved_usd - reserved)
            self._settled_usd += actual
        return actual

    async def release(self, reserved: float) -> None:
        """Failed attempts must not permanently consume budget nobody spent."""
        async with self._lock:
            self._reserved_usd = max(0.0, self._reserved_usd - reserved)

    async def reset(self) -> None:
        async with self._lock:
            self._settled_usd = 0.0
            self._reserved_usd = 0.0


class ConcurrencyLimiter:
    """Records its peak so our number can be compared against the mock's."""

    def __init__(self, max_concurrency: int) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self.max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._in_flight = 0
        self.peak_in_flight = 0

    def __call__(self):
        return self._slot()

    def _slot(self):
        limiter = self

        class _Slot:
            async def __aenter__(self):
                await limiter._semaphore.acquire()
                limiter._in_flight += 1
                limiter.peak_in_flight = max(limiter.peak_in_flight, limiter._in_flight)
                return limiter

            async def __aexit__(self, *exc):
                limiter._in_flight -= 1
                limiter._semaphore.release()
                return False

        return _Slot()

    @property
    def in_flight(self) -> int:
        return self._in_flight


class RateLimiter:
    """At most `max_requests` calls start in any `window_s`.

    A sliding log of start times rather than a token bucket. A bucket that holds
    `max_requests` tokens and refills at `max_requests / window_s` spends its
    full bucket at once and then keeps refilling, so it can start nearly twice
    the limit inside one window -- which a provider counting a sliding window
    answers with 429s.
    """

    def __init__(self, max_requests: int, window_s: float) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be >= 1")
        if window_s <= 0:
            raise ValueError("window_s must be > 0")
        self.max_requests = max_requests
        self.window_s = window_s
        self._started: deque[float] = deque()
        self._lock = asyncio.Lock()
        self.waited_s = 0.0

    async def acquire(self) -> None:
        # The lock spans the sleep. Waiters then queue in arrival order and wake
        # one at a time; released together, every one of them would see the same
        # freed slot and start at once.
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._started and now - self._started[0] >= self.window_s:
                    self._started.popleft()
                if len(self._started) < self.max_requests:
                    self._started.append(now)
                    return
                wait_s = self._started[0] + self.window_s - now
                self.waited_s += wait_s
                await asyncio.sleep(wait_s)

    @property
    def in_window(self) -> int:
        now = time.monotonic()
        return sum(1 for t in self._started if now - t < self.window_s)


@dataclass
class Limits:

    limiter: ConcurrencyLimiter
    ledger: SpendLedger
    # None when RATE_LIMIT_REQUESTS=0, so an unconfigured limit costs nothing
    # and cannot fail.
    rate: RateLimiter | None = None

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "Limits":
        settings = settings or get_settings()
        return cls(
            limiter=ConcurrencyLimiter(settings.max_concurrency),
            ledger=SpendLedger(settings.spend_ceiling_usd, max_tokens=settings.max_tokens),
            rate=(
                RateLimiter(settings.rate_limit_requests, settings.rate_limit_window_s)
                if settings.rate_limit_requests > 0
                else None
            ),
        )
