"""Stage 4 (testing a non-deterministic dependency) -- invariants and
properties. The participant writes this file.

At checkout this ships as a skeleton: the first test passes, the rest are
`pytest.fail` placeholders describing a property and leaving you to assert
it. On the solution branch they are filled in, which is what you are reading.

The distinction the file is teaching: the tests you have written so far check
examples -- this input gives that output. A property holds for every input,
and it is properties that survive a model swap, a temperature change or a
prompt rewrite. When the vendor ships a new checkpoint next quarter, your
example tests all go red and tell you nothing; these still mean something.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.schema import TriageResult
from tests.conftest import SAMPLE_TEXT, SAMPLES_DIR

SAMPLES = sorted(SAMPLES_DIR.glob("*.txt"))
SEVERITIES = ["trivial", "low", "medium", "high", "critical"]


def test_schema_forbids_unknown_fields():
    """`extra="forbid"` is load-bearing: silent field drift is the enemy.

    Ships GREEN as the worked example of what the rest of this file should
    look like.
    """
    with pytest.raises(Exception):
        TriageResult.model_validate(
            {
                "severity": "high",
                "component": "auth",
                "steps": [],
                "confidence": 0.5,
                "urgency": "asap",          # not in the contract
            }
        )


# --- properties -------------------------------------------------------------


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda p: p.stem)
@pytest.mark.chaos
async def test_output_always_satisfies_the_contract(make_service, env, sample):
    """PROPERTY: every successful extraction validates, for every input.

    Not "the ten samples give sensible answers" -- that is the golden set's
    job. This says the shape is guaranteed regardless of what came in.
    """
    env(FAULT_RATE="0.4", TEMPERATURE="0.7", MAX_RETRIES="5")
    service = make_service()

    result, _ = await service.triage(sample.read_text())

    assert isinstance(result, TriageResult)
    assert result.severity in SEVERITIES
    assert isinstance(result.component, str) and result.component
    assert 0.0 <= result.confidence <= 1.0
    assert isinstance(result.steps, list)
    assert result.duplicate_of is None or isinstance(result.duplicate_of, str)
    # Round-tripping must be lossless, or the cache cannot promise byte-identity.
    assert TriageResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "\n\n\n",
        "?",
        "a" * 5000,
        "🔥" * 200,
        "null",
        "{}",
        '{"severity": "critical"}',
        "<script>alert(1)</script>",
        "-- DROP TABLE bugs; --",
        "\x00\x01\x02 binary-ish",
    ],
    ids=[
        "empty", "space", "newlines", "punct", "very-long", "emoji", "null-word",
        "empty-json", "partial-json", "html", "sql", "control-chars",
    ],
)
async def test_hostile_input_never_crashes_the_service(make_service, env, text):
    """PROPERTY: no input produces an unhandled exception.

    Either a valid TriageResult or a typed error the service layer knows how
    to turn into a status code. A KeyError or a bare ValueError escaping means
    some input can take the process down, and inputs come from users.
    """
    env(FAULT_RATE="0.0", TEMPERATURE="0.7", MAX_RETRIES="3")
    service = make_service()

    from app.providers.base import ProviderError
    from app.reliability.breaker import CircuitOpenError
    from app.reliability.limits import SpendCeilingExceeded
    from app.reliability.retry import RetryExhaustedError

    expected = (ProviderError, RetryExhaustedError, CircuitOpenError, SpendCeilingExceeded)

    try:
        result, _ = await service.triage(text)
    except expected:
        return                      # a known failure, correctly typed
    except Exception as exc:
        pytest.fail(f"unhandled {type(exc).__name__} for input {text[:40]!r}: {exc}")
    else:
        assert isinstance(result, TriageResult)


async def test_idempotency_is_reflexive(make_service, env):
    """PROPERTY: for any key, triage(text, key) == triage(text, key).

    The cache's contract stated as a law rather than as one example.
    """
    env(FAULT_RATE="0.0", TEMPERATURE="0.7")
    service = make_service()

    for i, sample in enumerate(SAMPLES[:4]):
        text = sample.read_text()
        first, _ = await service.triage(text, idem_key=f"prop-{i}")
        second, cached = await service.triage(text, idem_key=f"prop-{i}")
        assert cached is True
        assert first.model_dump_json() == second.model_dump_json()


@pytest.mark.chaos
async def test_retry_never_exceeds_its_budget(make_service, mock_server, env):
    """PROPERTY: upstream calls per request <= 1 + MAX_RETRIES, for any input.

    Stated over several inputs and several budgets, so it is a law about the
    retry layer rather than a fact about one request.
    """
    for max_retries in (0, 2):
        for sample in SAMPLES[:3]:
            env(
                FAULT_RATE="0.4",
                TEMPERATURE="0.7",
                MAX_RETRIES=str(max_retries),
                MAX_CONCURRENCY="4",
            )
            service = make_service()
            mock_server.reset_stats()
            try:
                await service.triage(sample.read_text())
            except Exception:
                pass                # failing is allowed; overspending is not
            calls = mock_server.stats()["calls"]
            assert calls <= 1 + max_retries, (
                f"{calls} upstream calls with MAX_RETRIES={max_retries} "
                f"for {sample.name}"
            )

    # "At most" alone is true of a retry that never retries. Against an upstream
    # that always fails transiently, the budget must be spent exactly.
    from app.reliability.retry import RetryExhaustedError

    for max_retries in (0, 2):
        env(FAULT_RATE="1.0", FAULT_MODE="rate_limit", TEMPERATURE="0.7",
            MAX_RETRIES=str(max_retries))
        service = make_service()
        mock_server.reset_stats()
        with pytest.raises(RetryExhaustedError):
            await service.triage(f"always failing, budget {max_retries}")
        assert mock_server.stats()["calls"] == 1 + max_retries


@pytest.mark.latency
async def test_concurrency_never_exceeds_the_limit(make_service, mock_server, env):
    """PROPERTY: the mock's peak concurrency <= MAX_CONCURRENCY, at any limit."""
    for limit in (1, 3):
        env(
            FAULT_RATE="0.0",
            TEMPERATURE="0.7",
            MAX_CONCURRENCY=str(limit),
            MOCK_LATENCY_S="0.15",
            REQUEST_TIMEOUT_S="20.0",
        )
        service = make_service()
        mock_server.reset_stats()

        await asyncio.gather(
            *(service.triage(f"{s.read_text()} run={limit}") for s in SAMPLES[:6])
        )

        peak = mock_server.stats()["peak_concurrency"]
        assert peak <= limit, f"peak {peak} exceeded MAX_CONCURRENCY={limit}"


@pytest.mark.chaos
async def test_a_stream_ends_in_exactly_one_terminal_event(make_service, env):
    """PROPERTY: every stream ends in exactly one of `done` or `error`.

    Never both, never neither. A stream with no terminal event leaves the
    caller waiting forever; one with both is telling two different stories.
    """
    env(FAULT_RATE="0.4", TEMPERATURE="0.7")
    service = make_service()

    for sample in SAMPLES[:5]:
        events = [event async for event in service.triage_stream(sample.read_text())]
        terminals = [event for event in events if event.type in {"done", "error"}]

        assert len(terminals) == 1, (
            f"{sample.name}: expected exactly one terminal event, got "
            f"{[event.type for event in terminals]}"
        )
        assert terminals[0] is events[-1], "the terminal event must be last"
        if terminals[0].type == "done":
            assert isinstance(terminals[0].result, TriageResult)
        else:
            assert terminals[0].result is None, "an error event must carry no result"


async def test_spend_is_monotonic_and_bounded(make_service, env):
    """PROPERTY: spend only ever increases, and never past the ceiling."""
    env(FAULT_RATE="0.0", TEMPERATURE="0.7")
    service = make_service()
    ledger = service.limits.ledger

    previous = ledger.spent_usd
    for sample in SAMPLES[:5]:
        await service.triage(sample.read_text())
        # Strictly up: every sample is a fresh, paid call. ">=" alone is true of
        # a ledger that records nothing.
        assert ledger.spent_usd > previous, "a paid call did not increase spend"
        assert ledger.spent_usd <= ledger.ceiling_usd
        previous = ledger.spent_usd
