"""Settings, read from the environment with defaults.

GIVEN. Every knob the workshop turns lives here. Nothing else in the service
reads `os.environ` directly, so a participant can always answer "what is this
run actually configured to do?" by printing `get_settings()`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlparse

# Haiku 4.5 list price, USD per million tokens. Used by the mock's /stats and
# by the spend ceiling so both sides agree on what a call costs.
PRICE_INPUT_USD_PER_MTOK = 1.0
PRICE_OUTPUT_USD_PER_MTOK = 5.0

# The vendors' production API hosts. Traffic to either is real and billable.
# Both are listed because either dialect can be pointed at either host -- the
# guard keys on where the traffic goes, not on which adapter sends it.
REAL_API_HOSTS = ("api.anthropic.com", "api.openai.com")

# The dialects there are adapters for. PROVIDER names one of these; it does not
# name a Python class, and it does not decide the transport -- PROVIDER_BASE_URL
# does that, which is how either dialect can be pointed at the local mock.
PROVIDERS = ("anthropic", "openai")

_TRUE = {"1", "true", "yes", "on"}


def _str(key: str, default: str) -> str:
    value = os.environ.get(key)
    return default if value is None or value == "" else value


def _int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc


def _float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a number, got {raw!r}") from exc


def _bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in _TRUE


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the environment for one process."""

    # --- provider ---------------------------------------------------------
    # One adapter per dialect (providers/anthropic_p.py, providers/openai_p.py),
    # each driving its vendor's real SDK. PROVIDER names the dialect, not a Python class;
    # PROVIDER_BASE_URL alone chooses the transport. Pointed at the local mock
    # (the default) participants exercise the genuine SDK — its stream parsing,
    # its typed errors, its built-in retry behaviour — with no API key and no
    # network. Pointed at api.anthropic.com the same code path talks to the
    # real API. Swapping the transport must never swap the client.
    provider: str = field(default_factory=lambda: _str("PROVIDER", "anthropic"))
    fallback: str = field(default_factory=lambda: _str("FALLBACK_PROVIDER", ""))
    # Set-but-empty means "no base URL" -> the SDK's own default -> the real
    # API. Only an unset variable falls back to the mock, so clearing the value
    # does what the participant plainly meant rather than silently staying local.
    provider_base_url: str = field(
        default_factory=lambda: os.environ.get("PROVIDER_BASE_URL", "http://localhost:8081")
    )
    # One key per vendor. `api_key` is Anthropic's -- the name predates stage 7,
    # and renaming it would move stage 1's goalposts. Sharing one variable
    # between dialects would send an Anthropic key to OpenAI the moment
    # PROVIDER changed, which fails at best and leaks a credential at worst.
    api_key: str = field(default_factory=lambda: _str("ANTHROPIC_API_KEY", "not-a-real-key"))
    openai_api_key: str = field(default_factory=lambda: _str("OPENAI_API_KEY", "not-a-real-key"))
    model: str = field(default_factory=lambda: _str("MODEL", "claude-haiku-4-5"))
    temperature: float = field(default_factory=lambda: _float("TEMPERATURE", 0.0))
    max_tokens: int = field(default_factory=lambda: _int("MAX_TOKENS", 1024))

    # --- reliability ------------------------------------------------------
    max_retries: int = field(default_factory=lambda: _int("MAX_RETRIES", 3))
    request_timeout_s: float = field(default_factory=lambda: _float("REQUEST_TIMEOUT_S", 8.0))
    first_token_timeout_s: float = field(
        default_factory=lambda: _float("FIRST_TOKEN_TIMEOUT_S", 1.5)
    )
    max_concurrency: int = field(default_factory=lambda: _int("MAX_CONCURRENCY", 4))
    spend_ceiling_usd: float = field(default_factory=lambda: _float("SPEND_CEILING_USD", 1.0))
    # At most RATE_LIMIT_REQUESTS upstream calls in any RATE_LIMIT_WINDOW_S.
    # Off (0) by default: the real number is your account tier's, and the mock
    # enforces nothing unless MOCK_RATE_LIMIT_REQUESTS is set too. The window is
    # configurable only so the tests need not wait a minute to see one roll over.
    rate_limit_requests: int = field(default_factory=lambda: _int("RATE_LIMIT_REQUESTS", 0))
    rate_limit_window_s: float = field(default_factory=lambda: _float("RATE_LIMIT_WINDOW_S", 60.0))

    # --- mock fault injection --------------------------------------------
    fault_rate: float = field(default_factory=lambda: _float("FAULT_RATE", 0.0))
    fault_mode: str = field(default_factory=lambda: _str("FAULT_MODE", "all"))
    seed: int = field(default_factory=lambda: _int("SEED", 1337))

    # Explicit opt-in for real, billable traffic. Off by default.
    allow_real_api: bool = field(default_factory=lambda: _bool("ALLOW_REAL_API", False))

    # --- infrastructure ---------------------------------------------------
    redis_url: str = field(default_factory=lambda: _str("REDIS_URL", "redis://localhost:6379/0"))
    use_fakeredis: bool = field(default_factory=lambda: _bool("USE_FAKEREDIS", True))

    def __post_init__(self) -> None:
        if not 0.0 <= self.fault_rate <= 1.0:
            raise ValueError(f"FAULT_RATE must be between 0 and 1, got {self.fault_rate}")
        if not 0.0 <= self.temperature <= 1.0:
            raise ValueError(f"TEMPERATURE must be between 0 and 1, got {self.temperature}")
        if self.max_concurrency < 1:
            raise ValueError(f"MAX_CONCURRENCY must be >= 1, got {self.max_concurrency}")
        if self.rate_limit_requests < 0:
            raise ValueError(
                f"RATE_LIMIT_REQUESTS must be >= 0 (0 disables it), got {self.rate_limit_requests}"
            )
        if self.rate_limit_window_s <= 0:
            raise ValueError(f"RATE_LIMIT_WINDOW_S must be > 0, got {self.rate_limit_window_s}")
        if self.max_retries < 0:
            raise ValueError(f"MAX_RETRIES must be >= 0, got {self.max_retries}")
        if self.provider not in PROVIDERS:
            raise ValueError(
                f"PROVIDER must be one of {sorted(PROVIDERS)}, got {self.provider!r}. "
                "It names the dialect, not a Python class; PROVIDER_BASE_URL still "
                "chooses the transport."
            )
        if self.targets_real_api and not self.allow_real_api:
            # A base URL is a legitimate configuration — it is how the mock is
            # reached. What needs confirming is real, billable traffic, so the
            # guard keys on the HOST, not on the mere presence of a base URL.
            where = urlparse(self.provider_base_url).hostname or "the SDK's default host"
            raise ValueError(
                f"PROVIDER_BASE_URL points at {where}, which sends real, "
                "billable traffic and needs a real API key. Nothing in this "
                "workshop requires it. Set ALLOW_REAL_API=true to confirm."
            )


    @property
    def targets_real_api(self) -> bool:
        """An empty base URL means the SDK default, which is the real API."""
        if not self.provider_base_url:
            return True  # no base URL -> SDK default -> the real API
        host = urlparse(self.provider_base_url).hostname or ""
        return any(host == real or host.endswith(f".{real}") for real in REAL_API_HOSTS)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached, so one process cannot disagree with itself about its own config.
    Tests that change the environment must call reset_settings()."""
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
