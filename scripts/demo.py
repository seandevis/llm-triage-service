"""Demo scenarios for the recorded M5 session. One command, one point.

    python scripts/demo.py <scenario>

Each scenario starts the mock in-process, runs one small story against the
service, and prints what the provider saw. Nothing to start beforehand, and no
key: everything runs against the local mock.

    extract     stage 1  one report in, one validated object out
    limits      stage 2  ten requests: how many were in flight at once
    rate        stage 2  a burst against a provider limit of 4 per second
    stream      stage 3  a streamed answer, event by event
    cut         stage 3  a stream cut off mid-object
    chaos       stage 5  30 requests at FAULT_RATE=0.4: what got through, what it cost
    budget      stage 5  an upstream that always fails, at MAX_RETRIES 0..3
    replay      stage 5  8 identical requests with one idempotency key
    ceiling     stage 5  a spend ceiling smaller than the work
    outage      stage 6  the provider is down: what the breaker saves
    openai      stage 7  the chaos run again, over the OpenAI dialect
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.config as config_module  # noqa: E402
from tests.conftest import SAMPLES_DIR, MockServer, _free_port  # noqa: E402

SAMPLES = sorted(SAMPLES_DIR.glob("*.txt"))
REPORTS = [
    "Login fails with a 500 for every user after the deploy.",
    "Refund is charged twice when the button is pressed twice.",
    "The settings modal is misaligned by 40px on the dashboard.",
    "Order page takes 9 seconds intermittently, possibly a deadlock.",
    "Search returns unrelated articles for two-word queries.",
    "Comment notification emails arrive up to 20 minutes late.",
    "Uploading a 200MB attachment fails at the very end.",
    "Worker cannot connect to postgres, OperationalError in the log.",
    "TypeError on checkout: NoneType multiplied by int in totals.",
    "The export button does nothing on the reports screen.",
]


# --- plumbing -----------------------------------------------------------------

def configure(**env: str) -> None:
    """Fresh settings for this scenario, the way a restart would read them."""
    base = {
        "PROVIDER": "anthropic", "MODEL": "claude-haiku-4-5",
        "TEMPERATURE": "0.7", "FAULT_RATE": "0.0", "FAULT_MODE": "all", "SEED": "1337",
        "MAX_RETRIES": "3", "MAX_CONCURRENCY": "4", "REQUEST_TIMEOUT_S": "8.0",
        "SPEND_CEILING_USD": "1.0", "MOCK_LATENCY_S": "0", "MOCK_TIMEOUT_HOLD_S": "2.0",
        "RATE_LIMIT_REQUESTS": "0", "MOCK_RATE_LIMIT_REQUESTS": "0",
    }
    base.update(env)
    os.environ.update(base)
    config_module.reset_settings()


def service():
    from app.service import TriageService

    return TriageService(settings=config_module.get_settings())


def heading(text: str) -> None:
    print(f"\n\033[1m{text}\033[0m")


def show_stats(stats: dict, *, requests: int | None = None) -> None:
    outcomes = ", ".join(f"{k} {v}" for k, v in sorted(stats["calls_by_outcome"].items()))
    print(f"  upstream calls      {stats['calls']}" + (f"  for {requests} requests" if requests else ""))
    print(f"  outcomes            {outcomes or 'none'}")
    print(f"  retries observed    {stats['retries_observed']}")
    print(f"  peak concurrency    {stats['peak_concurrency']}")
    print(f"  cost                ${stats['estimated_cost_usd']:.6f}")


# --- stage 1 ------------------------------------------------------------------

async def extract(mock: MockServer) -> None:
    configure()
    svc = service()
    text = SAMPLES[0].read_text()
    heading(f"Input ({SAMPLES[0].name}):")
    print("  " + text.strip().replace("\n", "\n  "))
    result, _ = await svc.triage(text)
    heading("Output, validated as TriageResult:")
    print("  " + json.dumps(result.model_dump(), indent=2).replace("\n", "\n  "))
    await svc.aclose()


# --- stage 2 ------------------------------------------------------------------

async def limits(mock: MockServer) -> None:
    configure(MOCK_LATENCY_S="0.8", MAX_CONCURRENCY="4")
    svc = service()
    mock.reset_stats()
    started = time.monotonic()
    await asyncio.gather(*(svc.triage(t) for t in REPORTS))
    elapsed = time.monotonic() - started
    stats = mock.stats()
    heading("10 requests, each taking 0.8s upstream, MAX_CONCURRENCY=4")
    print(f"  took                {elapsed:.1f}s   (one by one: ~8s; all at once: ~0.8s)")
    print(f"  provider saw        at most {stats['peak_concurrency']} in flight")
    await svc.aclose()


async def rate(mock: MockServer) -> None:
    configure(RATE_LIMIT_REQUESTS="4", RATE_LIMIT_WINDOW_S="1.0",
              MOCK_RATE_LIMIT_REQUESTS="4", MOCK_RATE_LIMIT_WINDOW_S="1.0",
              MAX_CONCURRENCY="10")
    svc = service()
    mock.reset_stats()
    started = time.monotonic()
    await asyncio.gather(*(svc.triage(t) for t in REPORTS))
    elapsed = time.monotonic() - started
    stats = mock.stats()
    heading("10 requests at once; the provider allows 4 per second")
    print(f"  took                {elapsed:.1f}s   (3 windows)")
    print(f"  throttled (429)     {stats['calls_by_outcome'].get('throttled', 0)}")
    print(f"  upstream calls      {stats['calls']}")
    await svc.aclose()


# --- stage 3 ------------------------------------------------------------------

async def _print_stream(svc, text: str) -> None:
    started = time.monotonic()
    async for event in svc.triage_stream(text):
        at = f"{time.monotonic() - started:5.2f}s"
        if event.type == "delta":
            print(f"  {at}  delta  {event.text!r}")
        elif event.type == "done":
            print(f"  {at}  done   component={event.result.component} severity={event.result.severity}")
        elif event.type == "error":
            print(f"  {at}  error  transient={event.transient}  {event.error[:60]}")
        else:
            print(f"  {at}  {event.type}")


async def stream(mock: MockServer) -> None:
    configure(MOCK_LATENCY_S="1.0")
    svc = service()
    heading("A streamed answer, event by event")
    await _print_stream(svc, REPORTS[0])
    await svc.aclose()


async def cut(mock: MockServer) -> None:
    configure(FAULT_RATE="1.0", FAULT_MODE="truncated_stream")
    svc = service()
    heading("The provider cuts the stream mid-object")
    await _print_stream(svc, REPORTS[0])
    print("  -> no 'done', no half object: the caller gets an error it can act on")
    await svc.aclose()


# --- stage 5 ------------------------------------------------------------------

async def chaos(mock: MockServer, *, provider: str = "anthropic") -> None:
    extra = {"PROVIDER": "openai", "MODEL": "gpt-4o-mini"} if provider == "openai" else {}
    configure(FAULT_RATE="0.4", REQUEST_TIMEOUT_S="1.0", **extra)
    svc = service()
    mock.reset_stats()
    texts = [f"{t} (#{i})" for i in range(3) for t in REPORTS]
    started = time.monotonic()
    results = await asyncio.gather(*(svc.triage(t) for t in texts), return_exceptions=True)
    elapsed = time.monotonic() - started
    ok = sum(1 for r in results if not isinstance(r, BaseException))
    heading(f"30 requests, FAULT_RATE=0.4, MAX_RETRIES=3 ({provider})")
    print(f"  answered            {ok} of 30   in {elapsed:.1f}s")
    show_stats(mock.stats(), requests=30)
    print(f"  our ledger          ${svc.limits.ledger.spent_usd:.6f}")
    await svc.aclose()


async def budget(mock: MockServer) -> None:
    from app.reliability.retry import RetryExhaustedError

    heading("The provider always answers 429. One request per row.")
    print("  MAX_RETRIES   upstream calls")
    for retries in range(4):
        configure(FAULT_RATE="1.0", FAULT_MODE="rate_limit", MAX_RETRIES=str(retries))
        svc = service()
        svc.policy.max_delay_s = 0.05      # the 429's Retry-After is 1s; keep the demo short
        mock.reset_stats()
        try:
            await svc.triage(f"always rate limited ({retries})")
        except RetryExhaustedError:
            pass
        print(f"  {retries:>11}   {mock.stats()['calls']:>14}")
        await svc.aclose()
    print("  -> exactly 1 + MAX_RETRIES. Two retry layers would make it 3x that.")


async def replay(mock: MockServer) -> None:
    configure()
    svc = service()
    mock.reset_stats()
    text = SAMPLES[1].read_text()
    results = await asyncio.gather(*(svc.triage(text, idem_key="order-42") for _ in range(8)))
    bodies = {r.model_dump_json() for r, _ in results}
    cached = sum(1 for _, c in results if c)
    heading("8 identical requests at once, one Idempotency-Key")
    print(f"  upstream calls      {mock.stats()['calls']}")
    print(f"  served from cache   {cached}")
    print(f"  distinct answers    {len(bodies)}   (byte-identical)")
    await svc.aclose()


async def ceiling(mock: MockServer) -> None:
    from app.reliability.limits import SpendCeilingExceeded

    configure(SPEND_CEILING_USD="0.01")
    svc = service()
    mock.reset_stats()
    done = refused = 0
    for i in range(40):
        try:
            await svc.triage(f"{REPORTS[i % 10]} (#{i})")
            done += 1
        except SpendCeilingExceeded:
            refused += 1
    stats = mock.stats()
    heading("40 requests against a $0.01 ceiling")
    print(f"  answered            {done}")
    print(f"  refused up front    {refused}   (never sent: no money moved)")
    print(f"  upstream calls      {stats['calls']}")
    print(f"  spent / ceiling     ${svc.limits.ledger.spent_usd:.6f} / $0.01")
    print(f"  worst case per call ${svc.limits.ledger.worst_case_usd():.6f}  (reserved before each call)")
    await svc.aclose()


# --- stage 6 ------------------------------------------------------------------

async def outage(mock: MockServer) -> None:
    from app.reliability.breaker import CircuitOpenError
    from app.reliability.retry import RetryExhaustedError

    configure(FAULT_RATE="1.0", FAULT_MODE="rate_limit", MAX_RETRIES="3")
    svc = service()
    svc.policy.max_delay_s = 0.05
    mock.reset_stats()
    heading("The provider is down. Six requests in a row.")
    print("  request   answer             breaker   upstream calls so far")
    for i in range(1, 7):
        try:
            await svc.triage(f"during the outage ({i})")
            answer = "200"
        except CircuitOpenError:
            answer = "503 circuit open"
        except RetryExhaustedError:
            answer = "503 gave up"
        print(f"  {i:>7}   {answer:<17}  {svc.breaker.state:<8}  {mock.stats()['calls']:>21}")
    print(f"  -> without the breaker: {6 * 4} calls into a dead provider")
    await svc.aclose()


async def openai(mock: MockServer) -> None:
    await chaos(mock, provider="openai")


SCENARIOS = {f.__name__: f for f in (
    extract, limits, rate, stream, cut, chaos, budget, replay, ceiling, outage, openai
)}


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in SCENARIOS:
        print(__doc__)
        sys.exit(2)
    mock = MockServer(_free_port())
    mock.start()
    # The mock severs streams on purpose, and uvicorn logs each one as a server
    # error with a traceback. That is the fault working, not the demo failing.
    logging.getLogger("uvicorn.error").disabled = True
    os.environ.update(PROVIDER_BASE_URL=mock.base_url, ANTHROPIC_API_KEY="not-a-real-key",
                      OPENAI_API_KEY="not-a-real-key", USE_FAKEREDIS="true")
    try:
        asyncio.run(SCENARIOS[sys.argv[1]](mock))
    finally:
        mock.stop()


if __name__ == "__main__":
    main()
