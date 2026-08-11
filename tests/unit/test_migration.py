from datetime import UTC, datetime

import fakeredis.aioredis

from ip_proxy_pool.migration import LegacyMigrator
from ip_proxy_pool.storage.repository import RedisRepository


async def test_dry_run_writes_nothing() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repository = RedisRepository(client, prefix="ippool:test")
    migrator = LegacyMigrator(
        client,
        repository,
        now=lambda: datetime(2026, 8, 10, tzinfo=UTC),
    )
    try:
        await client.zadd("proxies", {"1.1.1.1:80": 100})

        summary = await migrator.import_key("proxies", "example.com", dry_run=True)

        assert summary.accepted == 1
        assert summary.written == 0
        assert await repository.list_domains() == []
    finally:
        await client.aclose()


async def test_repeated_import_does_not_duplicate() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repository = RedisRepository(client, prefix="ippool:test")
    migrator = LegacyMigrator(
        client,
        repository,
        now=lambda: datetime(2026, 8, 10, tzinfo=UTC),
    )
    try:
        await client.zadd("proxies", {"1.1.1.1:80": 100})

        first = await migrator.import_key("proxies", "example.com")
        second = await migrator.import_key("proxies", "example.com")

        assert first.written == 1
        assert second.written == 0
        assert (await repository.stats("example.com")).total == 1
        stored = await repository.get_record("example.com", "1.1.1.1:80")
        assert stored is not None and stored.score == 70
    finally:
        await client.aclose()


async def test_summary_distinguishes_invalid_and_non_global() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repository = RedisRepository(client, prefix="ippool:test")
    migrator = LegacyMigrator(
        client,
        repository,
        now=lambda: datetime(2026, 8, 10, tzinfo=UTC),
    )
    try:
        await client.zadd(
            "proxies",
            {"1.1.1.1:80": 70, "127.0.0.1:80": 60, "bad": 50},
        )

        summary = await migrator.import_key("proxies", "example.com")

        assert summary.scanned == 3
        assert summary.accepted == 1
        assert summary.skipped_non_global == 1
        assert summary.skipped_invalid == 1
    finally:
        await client.aclose()
