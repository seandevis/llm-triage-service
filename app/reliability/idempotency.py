"""Idempotency with a result cache.

Caches the result, not merely the fact that a call happened -- which is the part
most first attempts miss.

Ask a live model the same question twice at TEMPERATURE=0.7 and you get two
different answers, both valid. A caller retrying a request whose response it
never saw must get back the answer it already paid for, byte for byte. So the
same key returns an identical object, the second call does not reach upstream at
all, and two concurrent calls with one key produce one upstream call.

That last point is why this holds a per-key lock rather than doing a bare
check-then-set: N simultaneous retries would otherwise all miss the empty cache
together and all call upstream, which is the very case duplicates arrive in.

Storage is Redis (fakeredis on the no-Docker path) holding serialised JSON. An
in-process dict would pass the single-process test and fail in production.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any, Awaitable, Callable

from app.config import Settings, get_settings
from app.schema import TriageResult

# How long a cached result stays valid. Long enough to cover any realistic
# client retry window, short enough that the cache is not a database.
DEFAULT_TTL_S = 24 * 60 * 60

KEY_PREFIX = "triage:idem:"


def make_key(text: str, *, idem_key: str | None = None, salt: str = "") -> str:
    """The cache key for one logical request.

    An explicit `idem_key` from the caller wins: it is the caller telling us
    "these two requests are the same one", which is a stronger statement than
    identical bytes. Absent one, identical input text is the fallback identity.
    """
    if idem_key:
        digest = hashlib.sha256(f"{salt}:{idem_key}".encode()).hexdigest()
    else:
        digest = hashlib.sha256(f"{salt}:body:{text}".encode()).hexdigest()
    return f"{KEY_PREFIX}{digest}"


class IdempotencyStore:

    def __init__(self, redis: Any, *, ttl_s: int = DEFAULT_TTL_S) -> None:
        self.redis = redis
        self.ttl_s = ttl_s
        # Per-key locks collapse concurrent duplicates into one upstream call.
        # Process-local: within one worker this is exact. Across workers Redis
        # SET NX would be the next step; the workshop stays single-worker.
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    async def get(self, key: str) -> TriageResult | None:
        raw = await self.redis.get(key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return TriageResult.model_validate_json(raw)

    async def put(self, key: str, result: TriageResult) -> None:
        """Store `result` under `key`.

        Serialised once, here. Every later read returns exactly these bytes,
        which is what makes "byte-identical" true rather than merely "equal".
        """
        await self.redis.set(key, result.model_dump_json(), ex=self.ttl_s)

    async def get_or_call(
        self, key: str, operation: Callable[[], Awaitable[TriageResult]]
    ) -> tuple[TriageResult, bool]:
        """Return the cached result for `key`, or run `operation` and cache it.

        Returns `(result, cached)` so callers can tell a served-from-cache
        answer from a fresh one -- the spend ledger must not charge twice for
        one upstream call, and the API surfaces it as a header.

        The double-check inside the lock is deliberate: the first waiter runs
        the operation, everyone behind it finds the result already there.
        """
        hit = await self.get(key)
        if hit is not None:
            return hit, True

        lock = await self._lock_for(key)
        async with lock:
            hit = await self.get(key)
            if hit is not None:
                return hit, True
            result = await operation()
            await self.put(key, result)
            return result, False

    async def clear(self) -> None:
        """Tests only."""
        keys = await self.redis.keys(f"{KEY_PREFIX}*")
        if keys:
            await self.redis.delete(*keys)
        self._locks.clear()


def build_redis(settings: Settings | None = None) -> Any:
    """A Redis client, real or fake.

    `USE_FAKEREDIS=true` is the no-Docker path and the default: the tests must
    run with nothing installed but pip packages.
    """
    settings = settings or get_settings()
    if settings.use_fakeredis:
        import fakeredis.aioredis

        return fakeredis.aioredis.FakeRedis(decode_responses=True)
    import redis.asyncio

    return redis.asyncio.from_url(settings.redis_url, decode_responses=True)
