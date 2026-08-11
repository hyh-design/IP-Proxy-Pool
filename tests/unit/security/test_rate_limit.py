import fakeredis.aioredis

from ip_proxy_pool.security.rate_limit import RedisRateLimiter


async def test_limit_rejects_request_after_budget() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    limiter = RedisRateLimiter(client, prefix="ippool:test")
    try:
        assert (await limiter.check("query", "key:abc", 2, 60, now=1)).allowed
        assert (await limiter.check("query", "key:abc", 2, 60, now=2)).allowed
        denied = await limiter.check("query", "key:abc", 2, 60, now=3)

        assert denied.allowed is False
        assert denied.remaining == 0
        assert denied.retry_after_seconds == 57
    finally:
        await client.aclose()


async def test_scopes_and_subjects_have_separate_budgets() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    limiter = RedisRateLimiter(client, prefix="ippool:test")
    try:
        assert (await limiter.check("query", "a", 1, 60, now=1)).allowed
        assert (await limiter.check("query", "b", 1, 60, now=1)).allowed
        assert (await limiter.check("admin", "a", 1, 60, now=1)).allowed
        assert not (await limiter.check("query", "a", 1, 60, now=2)).allowed
    finally:
        await client.aclose()


async def test_next_fixed_window_resets_budget() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    limiter = RedisRateLimiter(client, prefix="ippool:test")
    try:
        assert (await limiter.check("query", "a", 1, 60, now=59)).allowed
        assert (await limiter.check("query", "a", 1, 60, now=60)).allowed
    finally:
        await client.aclose()


async def test_storage_keys_do_not_embed_subject() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    limiter = RedisRateLimiter(client, prefix="ippool:test")
    try:
        await limiter.check("query", "raw-secret", 1, 60, now=1)
        keys = await client.keys("*")

        assert len(keys) == 1
        assert "raw-secret" not in keys[0]
    finally:
        await client.aclose()
