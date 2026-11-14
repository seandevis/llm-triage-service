"""Shared fixtures: the mock running in-process, and a service pointed at it.

Two rules live here, because they are the ones most tempting to break when a
test goes red.

No network, no API key. The mock runs as a uvicorn server on a loopback port
inside the test process, so the SDK's real parsing, error types and stream
handling are genuinely exercised while nothing leaves the machine.

No test may silence chaos. The x-mock-fault-* headers may isolate one fault
type; they may never lower a rate a test is supposed to survive. Turning
FAULT_RATE=0.4 down to 0 with a header is not passing the test, it is deleting
it -- and make test-graded refuses the headers outright.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import threading
import time
from pathlib import Path
from typing import AsyncIterator, Iterator

import httpx
import pytest
import uvicorn

import app.config as config_module
from app.config import Settings
from app.reliability.idempotency import IdempotencyStore
from app.reliability.limits import Limits
from app.service import TriageService, build_provider

# Stage markers and the grouped failure summary. Registering it here rather
# than in pytest.ini keeps the whole test harness importable as a package.
pytest_plugins = ["tests.plugin_stages"]

# The repo root, resolved once. Tests import this rather than counting
# `.parent` hops -- the count is wrong the moment a file moves between
# tests/, tests/stages/ and tests/support/, and it moved once already.
REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLES_DIR = REPO_ROOT / "samples"
APP_DIR = REPO_ROOT / "app"
TESTS_DIR = REPO_ROOT / "tests"

# The suite's default chaos level. The gate runs at FAULT_RATE=0.4 and
# TEMPERATURE=0.7; these defaults make a bare `pytest` do the same thing, so a
# green local run means the same thing as a green gate run.
DEFAULT_FAULT_RATE = "0.4"
DEFAULT_TEMPERATURE = "0.7"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockServer:
    """The mock, running for real on a loopback port for the whole session."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        from mock.server import app as mock_app

        cfg = uvicorn.Config(
            mock_app, host="127.0.0.1", port=self.port, log_level="error", access_log=False
        )
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if getattr(self._server, "started", False):
                return
            time.sleep(0.02)
        raise RuntimeError("mock server did not start")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)

    # --- /stats helpers ---------------------------------------------------

    def reset_stats(self) -> None:
        httpx.post(f"{self.base_url}/stats/reset", timeout=10.0).raise_for_status()

    def stats(self) -> dict:
        response = httpx.get(f"{self.base_url}/stats", timeout=10.0)
        response.raise_for_status()
        return response.json()


@pytest.fixture(scope="session")
def mock_server() -> Iterator[MockServer]:
    server = MockServer(_free_port())
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(autouse=True)
def base_env(mock_server: MockServer, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point every test at the mock, at the gate's chaos level.

    Autouse so no test can accidentally run against a default that is easier
    than the gate. Individual tests narrow FAULT_MODE or raise MAX_RETRIES via
    `env` -- environment, not request headers.
    """
    monkeypatch.setenv("PROVIDER", "anthropic")
    monkeypatch.setenv("PROVIDER_BASE_URL", mock_server.base_url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("USE_FAKEREDIS", "true")
    monkeypatch.setenv("MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("TEMPERATURE", os.environ.get("TEMPERATURE", DEFAULT_TEMPERATURE))
    monkeypatch.setenv("FAULT_RATE", os.environ.get("FAULT_RATE", DEFAULT_FAULT_RATE))
    monkeypatch.setenv("FAULT_MODE", os.environ.get("FAULT_MODE", "all"))
    monkeypatch.setenv("SEED", os.environ.get("SEED", "1337"))
    monkeypatch.setenv("MAX_CONCURRENCY", "4")
    monkeypatch.setenv("MAX_RETRIES", "3")
    monkeypatch.setenv("MAX_TOKENS", "1024")
    monkeypatch.setenv("REQUEST_TIMEOUT_S", "8.0")
    monkeypatch.setenv("SPEND_CEILING_USD", "1.0")
    monkeypatch.delenv("MOCK_LATENCY_S", raising=False)
    # Rate limits on both sides start off; the stage-2 tests that need one set
    # it, so a limit left in a participant's shell cannot slow unrelated tests.
    for key in ("RATE_LIMIT_REQUESTS", "RATE_LIMIT_WINDOW_S",
                "MOCK_RATE_LIMIT_REQUESTS", "MOCK_RATE_LIMIT_WINDOW_S"):
        monkeypatch.delenv(key, raising=False)
    # The `timeout` fault holds the connection for 30s by default, and the
    # server keeps holding it after the client gives up -- teardown then waits
    # on it. Two seconds is still many times the sub-second REQUEST_TIMEOUT_S
    # the timeout tests set, so the stall is just as real and the suite does
    # not spend half a minute waiting for sockets nobody is reading.
    monkeypatch.setenv("MOCK_TIMEOUT_HOLD_S", os.environ.get("MOCK_TIMEOUT_HOLD_S", "2.0"))
    config_module.reset_settings()
    mock_server.reset_stats()
    yield
    config_module.reset_settings()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """Set env vars and drop the settings cache, the way a real deploy would.

    Use this rather than constructing Settings by hand: it exercises the same
    parsing path the service uses at boot.
    """

    def _set(**kwargs: object) -> Settings:
        for key, value in kwargs.items():
            monkeypatch.setenv(key, str(value))
        config_module.reset_settings()
        return config_module.get_settings()

    return _set


@pytest.fixture
def make_service(mock_server: MockServer):
    """Build a TriageService against the mock, with a clean cache and ledger."""
    created: list[TriageService] = []

    def _make(**overrides) -> TriageService:
        settings = overrides.pop("settings", None) or config_module.get_settings()
        service = TriageService(
            settings=settings,
            # build_provider rather than a hard-coded class, so PROVIDER selects
            # the dialect here exactly as it does in the running service.
            provider=overrides.pop("provider", None) or build_provider(settings),
            limits=overrides.pop("limits", None) or Limits.from_settings(settings),
            **overrides,
        )
        created.append(service)
        return service

    yield _make

    async def _close() -> None:
        for service in created:
            with contextlib.suppress(Exception):
                await service.aclose()

    asyncio.get_event_loop_policy()
    with contextlib.suppress(Exception):
        asyncio.run(_close())


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


SAMPLE_TEXT = (
    "Checkout fails with a 500 for every card payment since the 4.2 deploy.\n"
    "1. Add any item to the basket\n"
    "2. Go to checkout and enter a valid test card\n"
    "3. Press Pay\n"
    "Traceback shows a KeyError in the payments handler."
)


def header_overrides_allowed() -> bool:
    """Whether the mock will honour `x-mock-fault-*` headers in this run.

    `make test-graded` sets `MOCK_ALLOW_HEADER_OVERRIDE=false`. Tests that
    isolate one fault type via a header skip themselves rather than passing
    vacuously, and the same property is covered through FAULT_MODE besides.
    """
    raw = os.environ.get("MOCK_ALLOW_HEADER_OVERRIDE")
    if raw is None or raw == "":
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@pytest.fixture
async def app_client(mock_server: MockServer):
    """The FastAPI app over httpx's ASGI transport, plus its TriageService.

    In-process, so no port and no network -- but it goes through the real
    routing, validation and response handling, which is where the status-code
    mapping lives.
    """
    import httpx

    from app.main import app as fastapi_app

    settings = config_module.get_settings()
    service = TriageService(settings=settings)
    fastapi_app.state.settings = settings
    fastapi_app.state.service = service

    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        try:
            yield client, service
        finally:
            await service.aclose()


def adapter_implemented() -> bool:
    """Whether the adapter and the reliability layers are built yet.

    Supporting tests use this to skip rather than fail while the stubs are in
    place. The distinction matters for the participant's signal: a red test is
    an instruction to go and do something, and every red at checkout should be
    a stage file. A supporting test that fails only because a stub is still a
    stub is noise -- it tells them nothing a stage file has not already said,
    and it dilutes the list of things that actually need their attention.

    The stage-2 and stage-5 stubs pass calls straight through rather than
    raising, so that stage 1 can run through the whole service. That means
    "does it construct" no longer tells a stub from the real thing, so this
    probes behaviour instead: a semaphore that bounds, a retry that retries, a
    ledger that refuses, a cache that caches. Supporting tests come back once
    stages 1, 2 and 5 are real, and any failure then is a genuine regression.
    """
    from app.config import get_settings
    from app.providers.anthropic_p import AnthropicProvider

    try:
        AnthropicProvider(get_settings())
    except NotImplementedError:
        return False
    except Exception:
        return True          # built, but misconfigured -- that IS a real failure
    try:
        return asyncio.run(_reliability_layers_behave())
    except Exception:
        # Not built, or built wrong. Either way the stage files say so more
        # precisely than a supporting test would.
        return False


async def _reliability_layers_behave() -> bool:
    import fakeredis.aioredis

    from app.providers.base import TransientProviderError
    from app.reliability.idempotency import IdempotencyStore, make_key
    from app.reliability.limits import ConcurrencyLimiter, SpendCeilingExceeded, SpendLedger
    from app.reliability.retry import RetryPolicy, with_retry
    from app.schema import TriageResult

    limiter = ConcurrencyLimiter(1)
    async with limiter():
        second = asyncio.ensure_future(limiter().__aenter__())
        await asyncio.sleep(0.02)
        bounded = not second.done()
        second.cancel()
        with contextlib.suppress(BaseException):
            await second
    if not bounded:
        return False

    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransientProviderError("probe")
        return "ok"

    async def no_sleep(_: float) -> None:
        return None

    policy = RetryPolicy(max_retries=1, jitter=False)
    if await with_retry(flaky, policy=policy, sleep=no_sleep) != "ok":
        return False

    try:
        await SpendLedger(0.0, max_tokens=1024).reserve()
        return False
    except SpendCeilingExceeded:
        pass

    calls = 0

    async def answer() -> TriageResult:
        nonlocal calls
        calls += 1
        return TriageResult(severity="low", component="unknown", steps=[], confidence=0.5)

    store = IdempotencyStore(fakeredis.aioredis.FakeRedis(decode_responses=True))
    key = make_key("probe", idem_key="probe")
    await store.get_or_call(key, answer)
    _, cached = await store.get_or_call(key, answer)
    return cached and calls == 1


requires_adapter = pytest.mark.skipif(
    not adapter_implemented(),
    reason="adapter or reliability layers not implemented yet; this supporting test returns once they are",
)
