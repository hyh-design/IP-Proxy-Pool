import asyncio

import pytest
from redis.asyncio import Redis

from ip_proxy_pool.security.rate_limit import RedisRateLimiter


@pytest.mark.docker
async def test_real_redis_allows_exact_concurrent_budget(
    redis_client: Redis,
) -> None:
    limiter = RedisRateLimiter(redis_client, prefix="ippool:test")

    decisions = await asyncio.gather(
        *(limiter.check("query", "key:abc", 10, 60, now=1) for _ in range(50))
    )

    assert sum(decision.allowed for decision in decisions) == 10
