import fakeredis.aioredis

from ip_proxy_pool.checker.circuit_breaker import (
    CircuitState,
    RedisCircuitBreaker,
)


async def test_breaker_opens_after_three_shared_failures() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    breaker = RedisCircuitBreaker(client, prefix="ippool:test", failures=3, cooldown=60)
    try:
        for instant in (1.0, 2.0, 3.0):
            await breaker.record_failure("httpbin", now=instant)

        decision = await breaker.before_probe("httpbin", owner="worker-a", now=4.0)

        assert decision.allowed is False
        assert decision.state is CircuitState.OPEN
    finally:
        await client.aclose()


async def test_only_one_owner_gets_half_open_permission() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    breaker = RedisCircuitBreaker(client, prefix="ippool:test", failures=1, cooldown=60)
    try:
        await breaker.record_failure("httpbin", now=1.0)

        first = await breaker.before_probe("httpbin", owner="worker-a", now=61.0)
        second = await breaker.before_probe("httpbin", owner="worker-b", now=61.0)

        assert first.allowed is True
        assert first.state is CircuitState.HALF_OPEN
        assert second.allowed is False
        assert second.state is CircuitState.HALF_OPEN
    finally:
        await client.aclose()


async def test_success_resets_breaker_to_closed() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    breaker = RedisCircuitBreaker(client, prefix="ippool:test", failures=1, cooldown=60)
    try:
        await breaker.record_failure("httpbin", now=1.0)
        await breaker.before_probe("httpbin", owner="worker-a", now=61.0)
        await breaker.record_success("httpbin")

        decision = await breaker.before_probe("httpbin", owner="worker-b", now=62.0)

        assert decision.allowed is True
        assert decision.state is CircuitState.CLOSED
    finally:
        await client.aclose()


async def test_half_open_failure_restarts_cooldown() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    breaker = RedisCircuitBreaker(client, prefix="ippool:test", failures=1, cooldown=60)
    try:
        await breaker.record_failure("httpbin", now=1.0)
        await breaker.before_probe("httpbin", owner="worker-a", now=61.0)
        await breaker.record_failure("httpbin", now=62.0)

        decision = await breaker.before_probe("httpbin", owner="worker-b", now=100.0)

        assert decision.allowed is False
        assert decision.state is CircuitState.OPEN
    finally:
        await client.aclose()
