from datetime import UTC, datetime

import fakeredis.aioredis

from ip_proxy_pool import migration
from ip_proxy_pool.migration import LegacyMigrator
from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState
from ip_proxy_pool.storage.keys import keys_for
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


def indexed_record(address: str, *, state: ProxyState, latency: float | None) -> ProxyRecord:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    return ProxyRecord(
        endpoint=ProxyEndpoint.parse(address),
        domain="portal.daqihui.com",
        score=90,
        state=state,
        source_names={"rebuild-test"},
        first_seen_at=now,
        last_seen_at=now,
        last_checked_at=now,
        next_check_at=now,
        consecutive_successes=2,
        latency_ewma_ms=latency,
    )


async def test_latency_index_rebuild_is_exact_and_idempotent() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repository = RedisRepository(client, prefix="ippool:test")
    keys = keys_for("ippool:test", "portal.daqihui.com")
    try:
        await repository.save_record(
            indexed_record("1.1.1.1:80", state=ProxyState.AVAILABLE, latency=125.0)
        )
        await repository.save_record(
            indexed_record("8.8.8.8:80", state=ProxyState.DEGRADED, latency=100.0)
        )
        await repository.save_record(
            indexed_record("9.9.9.9:80", state=ProxyState.AVAILABLE, latency=None)
        )
        await client.delete(keys.available_latency, keys.available_latency_ready)
        rebuilder = migration.LatencyIndexRebuilder(client, prefix="ippool:test")

        first = await rebuilder.rebuild("portal.daqihui.com")
        second = await rebuilder.rebuild("portal.daqihui.com")

        assert first.scanned == second.scanned == 3
        assert first.indexed == second.indexed == 1
        assert first.ignored == second.ignored == 2
        assert await client.zrange(keys.available_latency, 0, -1, withscores=True) == [
            ("1.1.1.1:80", 125.0)
        ]
        assert await client.get(keys.available_latency_ready) == "1"
    finally:
        await client.aclose()


async def test_latency_index_dry_run_does_not_change_live_keys() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repository = RedisRepository(client, prefix="ippool:test")
    keys = keys_for("ippool:test", "portal.daqihui.com")
    try:
        await repository.save_record(
            indexed_record("1.1.1.1:80", state=ProxyState.AVAILABLE, latency=125.0)
        )
        await client.delete(keys.available_latency)
        await client.zadd(keys.available_latency, {"8.8.8.8:80": 999.0})
        await client.set(keys.available_latency_ready, "old")

        summary = await migration.LatencyIndexRebuilder(client, prefix="ippool:test").rebuild(
            "portal.daqihui.com", dry_run=True
        )

        assert summary.dry_run is True
        assert await client.zrange(keys.available_latency, 0, -1, withscores=True) == [
            ("8.8.8.8:80", 999.0)
        ]
        assert await client.get(keys.available_latency_ready) == "old"
    finally:
        await client.aclose()


async def test_empty_latency_index_rebuild_is_ready() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repository = RedisRepository(client, prefix="ippool:test")
    keys = keys_for("ippool:test", "portal.daqihui.com")
    try:
        await client.zadd(keys.available_latency, {"1.1.1.1:80": 100.0})

        summary = await migration.LatencyIndexRebuilder(client, prefix="ippool:test").rebuild(
            "portal.daqihui.com"
        )

        assert summary.indexed == 0
        assert await client.exists(keys.available_latency) == 0
        assert await client.get(keys.available_latency_ready) == "1"
        assert await repository.random_proxies(
            "portal.daqihui.com", min_score=80, count=20, max_latency_ms=1000
        ) == []
    finally:
        await client.aclose()
