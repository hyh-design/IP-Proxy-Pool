from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis

from ip_proxy_pool.dashboard.history import DashboardHistoryStore
from ip_proxy_pool.dashboard.models import (
    HistoryPoint,
    HistoryRange,
    HistoryResolution,
    LatencySummary,
    ScoreBuckets,
)

pytestmark = pytest.mark.docker
BASE = datetime(2026, 8, 1, tzinfo=UTC)


def point(observed_at: datetime, total: int) -> HistoryPoint:
    return HistoryPoint(
        observed_at=observed_at,
        total=total,
        candidate=0,
        available=total,
        degraded=0,
        quarantined=0,
        due=0,
        leased=0,
        availability_rate=1.0,
        available_pool_share=1.0,
        high_quality=total,
        score_buckets=ScoreBuckets(high=total),
        latency=LatencySummary(),
    )


async def test_history_retention_removes_index_and_payload_fields(redis_client: Redis) -> None:
    store = DashboardHistoryStore(
        redis_client,
        prefix="ippool:test",
        short_hours=48,
        long_hours=720,
    )
    short_start = BASE + timedelta(days=30)
    for index in range(577):
        await store.write(
            "example.com",
            HistoryResolution.FIVE_MINUTES,
            point(short_start + timedelta(minutes=5 * index), index),
        )
    for index in range(721):
        await store.write(
            "example.com",
            HistoryResolution.HOUR,
            point(BASE + timedelta(hours=index), index),
        )

    short = await store.read(
        "example.com",
        HistoryRange.H24,
        now=short_start + timedelta(minutes=5 * 577),
    )
    long = await store.read(
        "example.com",
        HistoryRange.D30,
        now=BASE + timedelta(hours=721),
    )
    short_keys = store.keys("example.com", HistoryResolution.FIVE_MINUTES)
    long_keys = store.keys("example.com", HistoryResolution.HOUR)

    assert len(short.points) == 288
    assert len(long.points) == 720
    assert await redis_client.zcard(short_keys.index) == 576
    assert await redis_client.hlen(short_keys.data) == 576
    assert await redis_client.zcard(long_keys.index) == 720
    assert await redis_client.hlen(long_keys.data) == 720
