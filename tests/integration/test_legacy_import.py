from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis

from ip_proxy_pool.migration import LegacyMigrator
from ip_proxy_pool.models import ProxyState
from ip_proxy_pool.storage.legacy import iter_legacy_records
from ip_proxy_pool.storage.repository import RedisRepository


@pytest.mark.docker
async def test_legacy_reader_does_not_change_old_zset(
    redis_client: Redis,
) -> None:
    await redis_client.zadd(
        "proxies",
        {
            "1.1.1.1:80": 100,
            "10.0.0.1:80": 90,
            "not-an-endpoint": 80,
        },
    )
    before = await redis_client.zrange("proxies", 0, -1, withscores=True)
    fixed_now = datetime(2026, 8, 10, tzinfo=UTC)

    records = [
        item
        async for item in iter_legacy_records(redis_client, "proxies", "example.com", fixed_now)
    ]

    assert len(records) == 1
    assert records[0].endpoint.canonical == "1.1.1.1:80"
    assert records[0].score == 70
    assert records[0].state is ProxyState.CANDIDATE
    assert records[0].next_check_at == fixed_now
    assert await redis_client.zrange("proxies", 0, -1, withscores=True) == before


@pytest.mark.docker
async def test_full_legacy_import_preserves_type_members_scores_and_ttl(
    redis_client: Redis,
) -> None:
    await redis_client.zadd("proxies", {"1.1.1.1:80": 100, "8.8.8.8:81": 60})
    await redis_client.expire("proxies", 600)
    before_type = await redis_client.type("proxies")
    before_members = await redis_client.zrange("proxies", 0, -1, withscores=True)
    before_ttl = await redis_client.ttl("proxies")
    repository = RedisRepository(redis_client, prefix="ippool:test")
    migrator = LegacyMigrator(
        redis_client,
        repository,
        now=lambda: datetime(2026, 8, 10, tzinfo=UTC),
    )

    summary = await migrator.import_key("proxies", "example.com")

    assert summary.written == 2
    assert await redis_client.type("proxies") == before_type
    assert await redis_client.zrange("proxies", 0, -1, withscores=True) == before_members
    after_ttl = await redis_client.ttl("proxies")
    assert before_ttl - 1 <= after_ttl <= before_ttl
