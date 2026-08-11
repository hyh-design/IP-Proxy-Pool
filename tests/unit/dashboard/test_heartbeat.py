from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest

from ip_proxy_pool.dashboard.heartbeat import RoleStatus, WorkerHeartbeatStore

BASE = datetime(2026, 8, 11, 8, tzinfo=UTC)


@pytest.fixture
async def redis() -> fakeredis.aioredis.FakeRedis:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def store(redis: fakeredis.aioredis.FakeRedis) -> WorkerHeartbeatStore:
    return WorkerHeartbeatStore(
        redis,
        prefix="ippool:test",
        interval_seconds=30,
        ttl_seconds=90,
    )


async def test_role_health_distinguishes_healthy_stale_and_down(
    store: WorkerHeartbeatStore,
) -> None:
    await store.beat("checker", "checker-a", now=BASE)

    healthy = await store.health("checker", now=BASE + timedelta(seconds=30))
    stale = await store.health("checker", now=BASE + timedelta(seconds=70))
    down = await store.health("checker", now=BASE + timedelta(seconds=91))

    assert healthy.status is RoleStatus.HEALTHY
    assert healthy.active_instances == 1
    assert stale.status is RoleStatus.STALE
    assert stale.active_instances == 1
    assert down.status is RoleStatus.DOWN
    assert down.active_instances == 0


async def test_heartbeat_index_is_capped_at_one_hundred_instances(
    redis: fakeredis.aioredis.FakeRedis,
    store: WorkerHeartbeatStore,
) -> None:
    for index in range(105):
        await store.beat("checker", f"worker-{index:03d}", now=BASE)

    assert await redis.zcard("ippool:test:worker-heartbeat:index:checker") == 100
    assert await redis.exists("ippool:test:worker-heartbeat:checker:worker-000") == 0
    assert await redis.exists("ippool:test:worker-heartbeat:checker:worker-104") == 1


@pytest.mark.parametrize("role", ["api", "redis", "", "CHECKER"])
async def test_heartbeat_rejects_unknown_roles(
    store: WorkerHeartbeatStore,
    role: str,
) -> None:
    with pytest.raises(ValueError, match="worker role"):
        await store.beat(role, "worker-a", now=BASE)


async def test_heartbeat_rejects_unsafe_instance_id_and_naive_time(
    store: WorkerHeartbeatStore,
) -> None:
    with pytest.raises(ValueError, match="instance ID"):
        await store.beat("collector", "bad/id", now=BASE)
    with pytest.raises(ValueError, match="timezone-aware"):
        await store.beat("collector", "collector-a", now=BASE.replace(tzinfo=None))
