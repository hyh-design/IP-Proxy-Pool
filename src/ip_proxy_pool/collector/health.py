import random
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

_BACKOFF_SECONDS = (30, 60, 120, 300, 600)


class SourceHealth:
    def __init__(
        self,
        redis: Any,
        *,
        prefix: str,
        jitter: Callable[[int, int], int] = random.randint,
    ) -> None:
        if not prefix:
            raise ValueError("prefix must be non-empty")
        self._redis = redis
        self._prefix = prefix
        self._jitter = jitter

    def _key(self, source_name: str) -> str:
        if not source_name:
            raise ValueError("source_name must be non-empty")
        return f"{self._prefix}:source-health:{quote(source_name, safe='.-_')}"

    async def can_run(self, source_name: str, *, now: float) -> bool:
        retry_at = await self._redis.hget(self._key(source_name), "retry_at")
        return retry_at is None or float(retry_at) <= now

    async def record_failure(self, source_name: str, *, now: float) -> int:
        key = self._key(source_name)
        failures = int(await self._redis.hincrby(key, "failure_count", 1))
        base = _BACKOFF_SECONDS[min(failures - 1, len(_BACKOFF_SECONDS) - 1)]
        delay = self._jitter(base, min(600, max(base, int(base * 1.2))))
        await self._redis.hset(key, mapping={"retry_at": now + delay})
        return delay

    async def record_success(self, source_name: str) -> None:
        await self._redis.delete(self._key(source_name))


class PredictionFailureCache:
    def __init__(self, redis: Any, *, prefix: str, ttl: int) -> None:
        if not prefix or ttl < 1:
            raise ValueError("prefix and ttl must be positive")
        self._redis = redis
        self._prefix = prefix
        self._ttl = ttl

    def _key(self, domain: str) -> str:
        if not domain:
            raise ValueError("domain must be non-empty")
        return f"{self._prefix}:precheck-fail:{quote(domain, safe='.-_')}"

    async def contains(self, domain: str, endpoint: str, *, now: float) -> bool:
        key = self._key(domain)
        expires_at = await self._redis.zscore(key, endpoint)
        if expires_at is None:
            return False
        if float(expires_at) <= now:
            await self._redis.zrem(key, endpoint)
            return False
        return True

    async def mark(self, domain: str, endpoint: str, *, now: float) -> None:
        await self._redis.zadd(self._key(domain), {endpoint: now + self._ttl})

    async def clear(self, domain: str, endpoint: str) -> None:
        await self._redis.zrem(self._key(domain), endpoint)
