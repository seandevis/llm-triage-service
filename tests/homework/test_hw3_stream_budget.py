"""Homework 3 -- streams inside the spend ceiling.

Red at checkout: `triage_stream` never touches the SpendLedger, so a client
that only streams has no spending limit at all. README's "Known gaps" says so.

Close it the way the unary path does: reserve the worst case before the stream
starts, settle the real cost when it ends, and give the reservation back if it
fails. The catch is that the real cost is only known at the end of the stream,
so the provider has to report its token usage somehow.
"""

from __future__ import annotations

import pytest

from app.reliability.limits import SpendCeilingExceeded

TEXT = "Checkout fails with a 500 for every card payment since the 4.2 deploy."


async def _drain(service, text: str = TEXT) -> list:
    return [event async for event in service.triage_stream(text)]


async def test_a_stream_is_charged(make_service, mock_server, env):
    env(FAULT_RATE="0.0", TEMPERATURE="0.7", SPEND_CEILING_USD="1.0")
    service = make_service()
    mock_server.reset_stats()

    events = await _drain(service)

    assert events[-1].type == "done", [e.type for e in events]
    spent = service.limits.ledger.spent_usd
    billed = mock_server.stats()["estimated_cost_usd"]
    assert spent > 0, "the stream cost money, and the ledger did not record it"
    assert spent == pytest.approx(billed, abs=1e-6), (
        f"the ledger says ${spent:.6f}, the provider billed ${billed:.6f}"
    )


async def test_a_stream_over_the_ceiling_is_refused_before_it_starts(make_service, mock_server, env):
    """Refuse before the request goes out, exactly like the unary path."""
    env(FAULT_RATE="0.0", TEMPERATURE="0.7", SPEND_CEILING_USD="0.001")
    service = make_service()
    mock_server.reset_stats()

    with pytest.raises(SpendCeilingExceeded):
        await _drain(service)
    assert mock_server.stats()["calls"] == 0, "nothing should reach the provider"


async def test_a_failed_stream_gives_its_reservation_back(make_service, env):
    env(FAULT_RATE="1.0", FAULT_MODE="truncated_stream", TEMPERATURE="0.7", SPEND_CEILING_USD="1.0")
    service = make_service()

    events = await _drain(service)

    assert events[-1].type == "error"
    ledger = service.limits.ledger
    assert ledger.committed_usd == pytest.approx(ledger.spent_usd, abs=1e-9), (
        "a reservation is still held after the stream failed; it must be released"
    )


async def test_streams_and_calls_share_one_budget(make_service, env):
    """One ceiling for the whole service, not one per path."""
    env(FAULT_RATE="0.0", TEMPERATURE="0.7", SPEND_CEILING_USD="1.0")
    service = make_service()

    await service.triage("Search returns unrelated articles for two-word queries.")
    after_call = service.limits.ledger.spent_usd
    await _drain(service)

    assert service.limits.ledger.spent_usd > after_call > 0
