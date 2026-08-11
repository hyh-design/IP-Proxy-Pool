from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest

from ip_proxy_pool.dashboard.history import DashboardHistoryStore, HistoryDataError
from ip_proxy_pool.dashboard.models import (
    HistoryPoint,
    HistoryRange,
    HistoryResolution,
    LatencySummary,
    ScoreBuckets,
)

BASE = datetime(2026, 8, 11, 8, tzinfo=UTC)


def history_point(observed_at: datetime, *, total: int) -> HistoryPoint:
    return HistoryPoint(
        observed_at=observed_at,
        total=total,
        candidate=0,
        available=total,
        degraded=0,
        quarantined=0,
        due=total,
        leased=0,
        availability_rate=1.0 if total else None,
        available_pool_share=1.0 if total else None,
        high_quality=total,
        score_buckets=ScoreBuckets(high=total),
        latency=LatencySummary(),
    )


@pytest.fixture
async def redis() -> fakeredis.aioredis.FakeRedis:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


async def test_history_read_uses_range_resolution_and_ascending_order(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    store = DashboardHistoryStore(redis, prefix="ippool:test", short_hours=48, long_hours=720)
    for minute in (0, 5, 10):
        await store.write(
            "example.com",
            HistoryResolution.FIVE_MINUTES,
            history_point(BASE + timedelta(minutes=minute), total=minute + 1),
        )

    result = await store.read("example.com", HistoryRange.H1, now=BASE + timedelta(hours=1))

    assert result.range is HistoryRange.H1
    assert result.resolution is HistoryResolution.FIVE_MINUTES
    assert [point.total for point in result.points] == [1, 6, 11]


async def test_history_read_does_not_fill_missing_buckets(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    store = DashboardHistoryStore(redis, prefix="ippool:test", short_hours=48, long_hours=720)
    await store.write(
        "example.com",
        HistoryResolution.FIVE_MINUTES,
        history_point(BASE, total=1),
    )
    await store.write(
        "example.com",
        HistoryResolution.FIVE_MINUTES,
        history_point(BASE + timedelta(minutes=10), total=2),
    )

    result = await store.read("example.com", HistoryRange.H1, now=BASE + timedelta(hours=1))

    assert [point.observed_at.minute for point in result.points] == [0, 10]


async def test_history_read_rejects_corrupt_payload(
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    store = DashboardHistoryStore(redis, prefix="ippool:test", short_hours=48, long_hours=720)
    keys = store.keys("example.com", HistoryResolution.FIVE_MINUTES)
    await redis.zadd(keys.index, {"202608110800": BASE.timestamp()})
    await redis.hset(keys.data, "202608110800", "not-json")

    with pytest.raises(HistoryDataError, match="invalid dashboard history"):
        await store.read("example.com", HistoryRange.H1, now=BASE + timedelta(hours=1))
