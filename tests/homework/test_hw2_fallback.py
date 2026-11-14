"""Homework 2 -- a second vendor as a fallback.

Red at checkout: when Anthropic is down, the service answers 503. Episode six
stopped us hammering a dead provider; episode seven proved OpenAI drops in as
one file. Put the two together: when the primary provider is down, answer from
the fallback instead.

Only for outages. A refusal or an answer that will not validate says nothing
about the provider's health, and the fallback would refuse the same request --
so a permanent error is not a reason to switch.

The tests drive the service with a fake primary that is always down, and the
real OpenAI adapter against the mock as the fallback.
"""

from __future__ import annotations

from typing import AsyncIterator

import pytest

import app.config as config_module
from app.providers.base import PermanentProviderError, TransientProviderError
from app.reliability.retry import RetryExhaustedError
from app.schema import StreamEvent, TriageResult
from app.service import TriageService


class DownProvider:
    """A primary that is always down. Counts how often it was asked."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error or TransientProviderError("primary is down")

    async def extract_with_usage(self, text: str, *, idem_key: str | None = None):
        self.calls += 1
        raise self.error

    async def extract(self, text: str, *, idem_key: str | None = None) -> TriageResult:
        result, _, _ = await self.extract_with_usage(text, idem_key=idem_key)
        return result

    async def stream(self, text: str) -> AsyncIterator[StreamEvent]:
        raise self.error
        yield  # pragma: no cover

    async def aclose(self) -> None:
        return None


def _openai_fallback():
    from app.providers.openai_p import OpenAIProvider

    return OpenAIProvider(config_module.get_settings())


def _service(make_service, **kwargs) -> TriageService:
    try:
        return make_service(**kwargs)
    except TypeError as exc:
        pytest.fail(
            f"homework 2: TriageService should accept a `fallback` provider ({exc})"
        )


async def test_the_fallback_answers_when_the_primary_is_down(make_service, mock_server, env):
    env(FAULT_RATE="0.0", TEMPERATURE="0.7", MAX_RETRIES="1")
    primary = DownProvider()
    service = _service(make_service, provider=primary, fallback=_openai_fallback())
    mock_server.reset_stats()

    result, _ = await service.triage("Login returns 500 for all users after the deploy")

    assert isinstance(result, TriageResult)
    assert primary.calls == 2, "the primary still gets its retries first: 1 + MAX_RETRIES"
    assert mock_server.stats()["calls"] == 1, "then one call to the fallback"


async def test_an_open_breaker_goes_straight_to_the_fallback(make_service, mock_server, env):
    """Once the breaker says the primary is down, don't even ask it."""
    env(FAULT_RATE="0.0", TEMPERATURE="0.7", MAX_RETRIES="1")
    primary = DownProvider()
    service = _service(make_service, provider=primary, fallback=_openai_fallback())
    for _ in range(service.breaker.failure_threshold):
        await service.breaker.record_failure()
    assert service.breaker.state == "open"
    mock_server.reset_stats()

    result, _ = await service.triage("Checkout fails with a 500 for every card payment")

    assert isinstance(result, TriageResult)
    assert primary.calls == 0, "the breaker is open: the primary must not be called"
    assert mock_server.stats()["calls"] == 1


async def test_a_permanent_error_does_not_fall_back(make_service, mock_server, env):
    env(FAULT_RATE="0.0", TEMPERATURE="0.7", MAX_RETRIES="3")
    primary = DownProvider(PermanentProviderError("provider refused to answer this input"))
    service = _service(make_service, provider=primary, fallback=_openai_fallback())
    mock_server.reset_stats()

    with pytest.raises(PermanentProviderError):
        await service.triage("a report the model declines to answer")
    assert primary.calls == 1
    assert mock_server.stats()["calls"] == 0, "a refusal is not an outage: no fallback call"


async def test_without_a_fallback_the_outage_still_surfaces(make_service, env):
    """Guard: the fallback is optional, and no fallback means the old behaviour."""
    env(FAULT_RATE="0.0", TEMPERATURE="0.7", MAX_RETRIES="1")
    service = make_service(provider=DownProvider())
    with pytest.raises(RetryExhaustedError):
        await service.triage("Login returns 500 for all users after the deploy")


async def test_the_fallback_answer_is_cached_like_any_other(make_service, mock_server, env):
    """The layers above still apply: a replayed key costs nothing, whoever answered."""
    env(FAULT_RATE="0.0", TEMPERATURE="0.7", MAX_RETRIES="1")
    service = _service(make_service, provider=DownProvider(), fallback=_openai_fallback())
    mock_server.reset_stats()

    first, cached_1 = await service.triage("Refund is charged twice", idem_key="hw2-1")
    second, cached_2 = await service.triage("Refund is charged twice", idem_key="hw2-1")

    assert (cached_1, cached_2) == (False, True)
    assert first.model_dump_json() == second.model_dump_json()
    assert mock_server.stats()["calls"] == 1


def test_the_fallback_is_chosen_by_a_setting(env):
    """FALLBACK_PROVIDER=openai builds one; unset means no fallback."""
    env(FALLBACK_PROVIDER="openai")
    service = TriageService(settings=config_module.get_settings())
    fallback = getattr(service, "fallback", "missing")
    if fallback == "missing":
        pytest.fail("homework 2: TriageService should expose the provider it falls back to as `.fallback`")
    assert type(fallback).__name__ == "OpenAIProvider"

    env(FALLBACK_PROVIDER="")
    assert TriageService(settings=config_module.get_settings()).fallback is None
