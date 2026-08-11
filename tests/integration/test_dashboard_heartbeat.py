from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis

from ip_proxy_pool.dashboard.heartbeat import RoleStatus, WorkerHeartbeatStore

pytestmark = pytest.mark.docker


async def test_real_redis_heartbeat_updates_ttl_and_index(redis_client: Redis) -> None:
    store = WorkerHeartbeatStore(
        redis_client,
        prefix="ippool:test",
        interval_seconds=30,
        ttl_seconds=90,
    )
    now = datetime.now(UTC)

    await store.beat("collector", "collector-a", now=now)
    health = await store.health("collector", now=now)

    assert health.status is RoleStatus.HEALTHY
    assert health.active_instances == 1
    assert await redis_client.ttl("ippool:test:worker-heartbeat:collector:collector-a") > 0
    assert await redis_client.zscore(
        "ippool:test:worker-heartbeat:index:collector", "collector-a"
    ) == pytest.approx(now.timestamp())
