import asyncio
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest

from ip_proxy_pool.config import DashboardSettings
from ip_proxy_pool.dashboard.history import DashboardHistoryStore
from ip_proxy_pool.dashboard.models import HistoryRange
from ip_proxy_pool.dashboard.snapshot import DashboardSnapshotRecorder
from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState
from ip_proxy_pool.storage.repository import RedisRepository

BASE = datetime(2026, 8, 11, 8, 7, tzinfo=UTC)


@pytest.fixture
async def redis() -> fakeredis.aioredis.FakeRedis:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


async def build_recorder(
    redis: fakeredis.aioredis.FakeRedis,
) -> tuple[DashboardSnapshotRecorder, DashboardHistoryStore, RedisRepository]:
    repository = RedisRepository(redis, prefix="ippool:test")
    record = ProxyRecord(
        endpoint=ProxyEndpoint.parse("1.1.1.1:80"),
        domain="example.com",
        score=95,
        state=ProxyState.AVAILABLE,
        source_names={"alpha"},
        first_seen_at=BASE,
        last_seen_at=BASE,
        last_checked_at=BASE,
        next_check_at=BASE + timedelta(minutes=5),
        consecutive_successes=2,
        latency_ewma_ms=100.0,
    )
    await repository.upsert_verified(record)
    settings = DashboardSettings()
    history = DashboardHistoryStore(
        redis,
        prefix="ippool:test",
        short_hours=settings.short_retention_hours,
        long_hours=settings.long_retention_hours,
    )
    recorder = DashboardSnapshotRecorder(
        redis,
        repository=repository,
        history=history,
        prefix="ippool:test",
        settings=settings,
    )
    return recorder, history, repository


async def test_concurrent_recorders_write_one_five_minute_bucket(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    first, history, repository = await build_recorder(redis)
    second = DashboardSnapshotRecorder(
        redis,
        repository=repository,
        history=history,
        prefix="ippool:test",
        settings=DashboardSettings(),
    )

    results = await asyncio.gather(first.record_if_due(BASE), second.record_if_due(BASE))
    bucket = BASE.replace(minute=5)
    series = await history.read("example.com", HistoryRange.H1, now=bucket + timedelta(hours=1))

    assert sorted(results) == [False, True]
    assert len(series.points) == 1
    assert series.points[0].observed_at == bucket
    assert series.points[0].available == 1


async def test_hour_boundary_writes_five_minute_and_hourly_history(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    recorder, history, _repository = await build_recorder(redis)
    on_hour = BASE.replace(hour=9, minute=0)

    assert await recorder.record_if_due(on_hour) is True

    short = await history.read("example.com", HistoryRange.H1, now=on_hour + timedelta(hours=1))
    long = await history.read("example.com", HistoryRange.D7, now=on_hour + timedelta(hours=1))
    assert [point.observed_at for point in short.points] == [on_hour]
    assert [point.observed_at for point in long.points] == [on_hour]
