"""The composition root: the order the reliability layers wrap the provider.

The order is the lesson. From outside in:

    idempotency  ->  spend ceiling  ->  breaker + retry  ->  semaphore  ->  SDK

  * Idempotency outermost. A cached result must cost nothing: no permit, no
    budget, no upstream call. Put it inside the semaphore and a burst of
    duplicate retries queues up to be told something we already knew.

  * Spend ceiling next. It reserves before the attempt, so the refusal
    happens before the money is spent. Note it wraps the *retry loop*: every
    attempt is charged, because every attempt costs real tokens. Charging only
    the successful one is how a retry storm goes unnoticed.

  * Breaker and retry together. Retry asks the breaker before each attempt,
    so an outage stops the loop instead of feeding it.

  * Semaphore innermost, held around the upstream call alone. It bounds what
    reaches the provider, which is the scarce thing.

  * Rate limit inside the semaphore, immediately before the call. Every
    attempt takes a slot, retries included, because the provider counts
    every request it receives.
"""

from __future__ import annotations

from typing import AsyncIterator

from app.config import Settings, get_settings
from app.providers.base import Provider
from app.reliability.breaker import CircuitBreaker, CircuitOpenError
from app.reliability.idempotency import IdempotencyStore, build_redis, make_key
from app.reliability.limits import Limits
from app.reliability.retry import RetryPolicy, with_retry, RetryExhaustedError
from app.schema import StreamEvent, TriageResult


def build_provider(settings: Settings, name: str | None = None) -> Provider | None:
    """The one place a dialect name becomes a concrete adapter.

    Imports are local so a process imports only the SDK of the dialect it is
    configured for. Until stage 7 is done, the OpenAI stub never loads for a
    service running the Anthropic dialect.

    Note what is NOT here: no reliability logic, no branching anywhere above
    this function. Everything from the semaphore upward is written against the
    Provider Protocol, so adding a dialect costs one adapter and one line here.
    """
    dialect = name if name is not None else settings.provider
    if dialect == "openai":
        from app.providers.openai_p import OpenAIProvider
        return OpenAIProvider(settings)
    elif dialect == "anthropic":
        from app.providers.anthropic_p import AnthropicProvider
        return AnthropicProvider(settings)
    return None

class TriageService:
    """Holds the provider and the reliability layers for the process."""

    def __init__(
        self,
        *,
        provider: Provider | None = None,
        fallback: Provider | None = None,
        settings: Settings | None = None,
        store: IdempotencyStore | None = None,
        limits: Limits | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        primary = provider or build_provider(self.settings)
        if primary is None:
            raise RuntimeError(f"no provider adapter for dialect {self.settings.provider!r}")
        self.provider: Provider = primary
        self.fallback = fallback or build_provider(self.settings, self.settings.fallback)
        self.store = store or IdempotencyStore(build_redis(self.settings))
        self.limits = limits or Limits.from_settings(self.settings)
        self.breaker = breaker or CircuitBreaker(name="anthropic")
        self.fallback_breaker = CircuitBreaker(name="openai") if self.fallback else None
        self.policy = RetryPolicy(max_retries=self.settings.max_retries)
        # Salts the cache key with the settings that change the answer, so a
        # run at a different temperature or model does not serve the previous
        # run's cached result.
        self._key_salt = f"{self.settings.model}:{self.settings.temperature}"

    # --- unary ------------------------------------------------------------

    async def triage(self, text: str, *, idem_key: str | None = None) -> tuple[TriageResult, bool]:
        """Triage `text`. Returns `(result, served_from_cache)`.

        With an idempotency key, the second call returns the stored object and
        makes no upstream call at all.
        """
        key = make_key(text, idem_key=idem_key, salt=self._key_salt)
        return await self.store.get_or_call(
            key, lambda: self._attempt_with_limits(text, idem_key)
        )

    async def _run_provider(self, provider, breaker, text, idem_key) -> TriageResult:
        async def one_attempt() -> TriageResult:
            # Reserve worst-case budget BEFORE the call. Raises rather than
            # reporting an overrun after the fact.
            reserved = await self.limits.ledger.reserve()
            settled = False
            try:
                async with self.limits.limiter():
                    await self._rate_slot()
                    result, in_tok, out_tok = await provider.extract_with_usage(
                        text, idem_key=idem_key
                    )
                await self.limits.ledger.settle(reserved, in_tok, out_tok)
                settled = True
                return result
            finally:
                if not settled:
                    await self.limits.ledger.release(reserved)
        return await with_retry(one_attempt, policy=self.policy, breaker=breaker)

    async def _attempt_with_limits(self, text, idem_key):
        try:
            return await self._run_provider(self.provider, self.breaker, text, idem_key)
        except (RetryExhaustedError, CircuitOpenError):
            if self.fallback is None:
                raise
            return await self._run_provider(self.fallback, self.fallback_breaker, text, idem_key)

    # --- streaming --------------------------------------------------------

    async def triage_stream(self, text: str) -> AsyncIterator[StreamEvent]:
        """Stream a triage result.

        No retry and no cache on this path: a stream has already started
        delivering bytes to the caller, so a transparent retry would replay a
        different answer mid-response. A truncated stream surfaces as an
        `error` event, which is what `test_stream.py` asserts.

        Not charged against the spend ceiling: the stream does not report its
        token usage, so there is nothing to reserve or settle. A known gap, not
        an oversight -- README's "Known gaps" says what closing it would take.

        The caller is expected to have run `breaker.before_call()` already --
        the route does, so it can answer 503 with a real status code before any
        bytes go out. Once a StreamingResponse has started there is no status
        code left to change.
        """
        reserved = await self.limits.ledger.reserve()
        settled = False
        try:
            async with self.limits.limiter():
                await self._rate_slot()
                upstream_failed = False
                async for event in self.provider.stream(text):
                    if event.type == "error" and event.transient is not False:
                        upstream_failed = True
                    if (
                        event.type == "done"
                        and event.input_tokens is not None
                        and event.output_tokens is not None
                    ):
                        await self.limits.ledger.settle(
                            reserved, event.input_tokens, event.output_tokens
                        )
                        settled = True
                    yield event
        finally:
            if not settled:
                await self.limits.ledger.release(reserved)
        # A permanent error still counts as success here: upstream answered,
        # and it was the request that could not be served. Only a transient
        # error is evidence that upstream is sick.
        if upstream_failed:
            await self.breaker.record_failure()
        else:
            await self.breaker.record_success()

    async def _rate_slot(self) -> None:
        if self.limits.rate is not None:
            await self.limits.rate.acquire()

    async def aclose(self) -> None:
        await self.provider.aclose()
