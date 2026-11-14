"""Retry with exponential backoff and jitter.

This is the only layer that retries. The anthropic SDK ships max_retries=2, so
providers/anthropic_p.py builds its client with 0; leave both on and one request
becomes up to (1 + MAX_RETRIES) x 3 upstream calls while MAX_RETRIES looks inert.
/stats shows it plainly as calls and retries_observed, which is why test_retry.py
asserts on the mock's call count rather than on whether the request succeeded.

Here rather than in the SDK because this layer can see the breaker, the ledger
and the cache: it stops retrying when the breaker opens and does not re-charge a
cached result. The SDK can see none of that, and the phase-two OpenAI adapter
would otherwise need the same logic written twice.

Only TransientProviderError is retried. A schema violation or a refusal fails
the same way four times over, costing four times as much to say so.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

from app.providers.base import TransientProviderError
from app.reliability.breaker import CircuitBreaker, CircuitOpenError

T = TypeVar("T")

BASE_DELAY_S = 0.05
MAX_DELAY_S = 2.0


@dataclass
class RetryPolicy:
    """How many times to try, and how long to wait between attempts.

    `max_retries` is the number of retries, so total attempts is
    `max_retries + 1`. MAX_RETRIES=0 means one attempt and no retrying, which
    is the setting `test_retry.py` uses to prove the SDK is not retrying
    behind our back.
    """

    max_retries: int = 3
    base_delay_s: float = BASE_DELAY_S
    max_delay_s: float = MAX_DELAY_S
    jitter: bool = True

    @property
    def max_attempts(self) -> int:
        return self.max_retries + 1

    def delay_for(self, attempt: int, retry_after: float | None = None) -> float:
        """Backoff before retry number `attempt` (1-based).

        A server-supplied `Retry-After` wins over our own backoff: it is the
        one party that actually knows when it will accept traffic again. We cap
        it so a hostile or buggy header cannot wedge the request past the
        client timeout.
        """
        if retry_after is not None:
            return max(0.0, min(retry_after, self.max_delay_s))
        delay = min(self.base_delay_s * (2 ** (attempt - 1)), self.max_delay_s)
        if self.jitter:
            # Full jitter. Without it, N clients that failed together retry
            # together and rebuild the thundering herd that caused the failure.
            delay = random.uniform(0.0, delay)
        return delay


class RetryExhaustedError(RuntimeError):
    """Every attempt failed transiently. Carries the last error."""

    def __init__(self, message: str, *, attempts: int, last_error: BaseException | None) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error


async def with_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    breaker: CircuitBreaker | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run `operation` until it succeeds, or until the retries run out.

    `operation` must perform exactly one upstream attempt. If it retries
    internally you have two layers again and this function cannot tell.
    """
    last_error: BaseException | None = None

    for attempt in range(1, policy.max_attempts + 1):
        if breaker is not None:
            # Raises CircuitOpenError, which is deliberately NOT caught below:
            # an open breaker means stop, not try harder.
            await breaker.before_call()
        try:
            result = await operation()
        except TransientProviderError as exc:
            last_error = exc
            if breaker is not None:
                await breaker.record_failure()
            if attempt >= policy.max_attempts:
                break
            await sleep(policy.delay_for(attempt, getattr(exc, "retry_after", None)))
            continue
        except CircuitOpenError:
            raise
        except Exception as exc:
            # Permanent: do not retry, do not trip the breaker. Upstream is
            # healthy; this particular request is not.
            last_error = exc
            raise
        else:
            if breaker is not None:
                await breaker.record_success()
            return result

    raise RetryExhaustedError(
        f"upstream still failing after {policy.max_attempts} attempt(s)",
        attempts=policy.max_attempts,
        last_error=last_error,
    ) from last_error
