import asyncio

import pytest
from redis.asyncio import Redis

from ip_proxy_pool.checker.circuit_breaker import (
    CircuitState,
    RedisCircuitBreaker,
)


@pytest.mark.docker
async def test_real_redis_allows_one_shared_half_open_owner(
    redis_client: Redis,
) -> None:
    first_breaker = RedisCircuitBreaker(redis_client, prefix="ippool:test", failures=1, cooldown=60)
    second_breaker = RedisCircuitBreaker(
        redis_client, prefix="ippool:test", failures=1, cooldown=60
    )
    await first_breaker.record_failure("httpbin", now=1.0)

    first, second = await asyncio.gather(
        first_breaker.before_probe("httpbin", owner="worker-a", now=61.0),
        second_breaker.before_probe("httpbin", owner="worker-b", now=61.0),
    )

    assert sorted([first.allowed, second.allowed]) == [False, True]
    assert {first.state, second.state} == {CircuitState.HALF_OPEN}
