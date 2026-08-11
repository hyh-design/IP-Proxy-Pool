import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis

from ip_proxy_pool.config import DashboardSettings
from ip_proxy_pool.dashboard.history import DashboardHistoryStore
from ip_proxy_pool.dashboard.models import HistoryRange
from ip_proxy_pool.dashboard.snapshot import DashboardSnapshotRecorder
from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState
from ip_proxy_pool.storage.repository import RedisRepository

pytestmark = pytest.mark.docker


async def test_real_redis_snapshot_is_idempotent_across_recorders(redis_client: Redis) -> None:
    now = datetime(2026, 8, 11, 8, 7, tzinfo=UTC)
    repository = RedisRepository(redis_client, prefix="ippool:test")
    await repository.upsert_verified(
        ProxyRecord(
            endpoint=ProxyEndpoint.parse("1.1.1.1:80"),
            domain="example.com",
            score=95,
            state=ProxyState.AVAILABLE,
            source_names={"alpha"},
            first_seen_at=now,
            last_seen_at=now,
            last_checked_at=now,
            next_check_at=now + timedelta(minutes=5),
            consecutive_successes=2,
        )
    )
    settings = DashboardSettings()
    history = DashboardHistoryStore(
        redis_client,
        prefix="ippool:test",
        short_hours=48,
        long_hours=720,
    )
    recorders = [
        DashboardSnapshotRecorder(
            redis_client,
            repository=repository,
            history=history,
            prefix="ippool:test",
            settings=settings,
        )
        for _ in range(2)
    ]

    result = await asyncio.gather(*(recorder.record_if_due(now) for recorder in recorders))
    bucket = now.replace(minute=5)
    series = await history.read("example.com", HistoryRange.H1, now=bucket + timedelta(hours=1))

    assert sorted(result) == [False, True]
    assert len(series.points) == 1
