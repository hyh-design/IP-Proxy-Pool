from typing import Any, cast

from ip_proxy_pool.reclaim_quota.lua import ACQUIRE
from ip_proxy_pool.reclaim_quota.models import QuotaDecision


class ReclaimQuotaStore:
    def __init__(
        self,
        redis: Any,
        *,
        prefix: str,
        minimum_redis_uptime_seconds: int = 65,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._minimum_uptime = minimum_redis_uptime_seconds

    async def acquire(self, domain: str, attempt_id: str) -> QuotaDecision:
        base = f"{self._prefix}:reclaim:global-quota:{domain}"
        result = cast(
            list[int],
            await self._redis.eval(
                ACQUIRE,
                2,
                f"{base}:window",
                f"{base}:attempt:{attempt_id}",
                attempt_id,
                str(self._minimum_uptime),
            ),
        )
        code, retry_after_ms, remaining = (int(value) for value in result)
        return QuotaDecision(
            granted=code == 1,
            retry_after_ms=retry_after_ms,
            remaining=remaining,
            unavailable=code == -1,
        )
