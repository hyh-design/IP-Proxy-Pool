import hashlib
import math
from dataclasses import dataclass
from typing import Any, cast

_INCREMENT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return {current, redis.call('TTL', KEYS[1])}
"""


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


class RedisRateLimiter:
    def __init__(self, redis: Any, *, prefix: str) -> None:
        if not prefix:
            raise ValueError("prefix must be non-empty")
        self._redis = redis
        self._prefix = prefix

    async def check(
        self,
        scope: str,
        subject: str,
        limit: int,
        window_seconds: int,
        *,
        now: float,
    ) -> RateLimitDecision:
        if not scope or not subject:
            raise ValueError("scope and subject must be non-empty")
        if limit < 1 or window_seconds < 1:
            raise ValueError("rate limit and window must be positive")

        window_number = int(now // window_seconds)
        digest_input = f"{scope}\0{subject}\0{window_number}".encode()
        digest = hashlib.sha256(digest_input).hexdigest()
        key = f"{self._prefix}:rate-limit:{digest}"
        result = cast(
            list[int],
            await self._redis.eval(_INCREMENT, 1, key, str(window_seconds)),
        )
        current = int(result[0])
        allowed = current <= limit
        window_end = (window_number + 1) * window_seconds
        retry_after = max(1, math.ceil(window_end - now))
        return RateLimitDecision(
            allowed=allowed,
            limit=limit,
            remaining=max(0, limit - current),
            retry_after_seconds=0 if allowed else retry_after,
        )
