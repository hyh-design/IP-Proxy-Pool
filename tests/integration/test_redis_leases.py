import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis

from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState
from ip_proxy_pool.storage.keys import keys_for
from ip_proxy_pool.storage.lua import (
    CLAIM_DUE,
    COMPLETE_LEASE,
    RECLAIM_EXPIRED,
    RELEASE_LEASE,
)
from ip_proxy_pool.storage.repository import Lease, RedisRepository


def make_record(address: str = "1.1.1.1:80") -> ProxyRecord:
    now = datetime.now(UTC)
    return ProxyRecord(
        endpoint=ProxyEndpoint.parse(address),
        domain="example.com",
        score=90,
        state=ProxyState.AVAILABLE,
        source_names={"integration"},
        first_seen_at=now,
        last_seen_at=now,
        last_checked_at=now,
        next_check_at=now + timedelta(minutes=5),
    )


@pytest.mark.docker
async def test_claim_moves_each_due_member_to_one_owner(
    redis_client: Redis,
) -> None:
    keys = keys_for("ippool:test", "example.com")
    await redis_client.zadd(keys.due, {"1.1.1.1:80": 1.0})

    first, second = await asyncio.gather(
        redis_client.eval(
            CLAIM_DUE,
            3,
            keys.due,
            keys.leased,
            keys.lease_owners,
            2,
            10,
            62,
            "w1:t1",
        ),
        redis_client.eval(
            CLAIM_DUE,
            3,
            keys.due,
            keys.leased,
            keys.lease_owners,
            2,
            10,
            62,
            "w2:t2",
        ),
    )

    assert sorted([len(first), len(second)]) == [0, 1]


@pytest.mark.docker
async def test_stale_owner_cannot_complete_a_lease(redis_client: Redis) -> None:
    keys = keys_for("ippool:test", "example.com")
    endpoint = "1.1.1.1:80"
    await redis_client.zadd(keys.leased, {endpoint: 62.0})
    await redis_client.hset(keys.lease_owners, endpoint, "worker-1")

    result = await redis_client.eval(
        COMPLETE_LEASE,
        5,
        keys.records,
        keys.quality,
        keys.due,
        keys.leased,
        keys.lease_owners,
        endpoint,
        "worker-2",
        '{"status":"new"}',
        0.9,
        100,
    )

    assert result == 0
    assert await redis_client.hget(keys.records, endpoint) is None
    assert await redis_client.zscore(keys.leased, endpoint) == 62.0
    assert await redis_client.hget(keys.lease_owners, endpoint) == "worker-1"


@pytest.mark.docker
async def test_completion_is_idempotent(redis_client: Redis) -> None:
    keys = keys_for("ippool:test", "example.com")
    endpoint = "1.1.1.1:80"
    record_json = '{"status":"available"}'
    await redis_client.zadd(keys.leased, {endpoint: 62.0})
    await redis_client.hset(keys.lease_owners, endpoint, "worker-1")

    arguments = (
        COMPLETE_LEASE,
        5,
        keys.records,
        keys.quality,
        keys.due,
        keys.leased,
        keys.lease_owners,
        endpoint,
        "worker-1",
        record_json,
        0.9,
        100,
    )
    first = await redis_client.eval(*arguments)
    second = await redis_client.eval(*arguments)

    assert (first, second) == (1, 0)
    assert await redis_client.hget(keys.records, endpoint) == record_json
    assert await redis_client.zscore(keys.quality, endpoint) == 0.9
    assert await redis_client.zscore(keys.due, endpoint) == 100.0
    assert await redis_client.zscore(keys.leased, endpoint) is None
    assert await redis_client.hget(keys.lease_owners, endpoint) is None


@pytest.mark.docker
async def test_release_preserves_record_and_quality(redis_client: Redis) -> None:
    keys = keys_for("ippool:test", "example.com")
    endpoint = "1.1.1.1:80"
    await redis_client.hset(keys.records, endpoint, '{"status":"existing"}')
    await redis_client.zadd(keys.quality, {endpoint: 0.4})
    await redis_client.zadd(keys.leased, {endpoint: 62.0})
    await redis_client.hset(keys.lease_owners, endpoint, "worker-1")

    result = await redis_client.eval(
        RELEASE_LEASE,
        3,
        keys.due,
        keys.leased,
        keys.lease_owners,
        endpoint,
        "worker-1",
        100,
    )

    assert result == 1
    assert await redis_client.hget(keys.records, endpoint) == '{"status":"existing"}'
    assert await redis_client.zscore(keys.quality, endpoint) == 0.4
    assert await redis_client.zscore(keys.due, endpoint) == 100.0
    assert await redis_client.zscore(keys.leased, endpoint) is None


@pytest.mark.docker
async def test_reclaim_makes_endpoint_claimable_and_rejects_old_owner(
    redis_client: Redis,
) -> None:
    keys = keys_for("ippool:test", "example.com")
    endpoint = "1.1.1.1:80"
    await redis_client.zadd(keys.leased, {endpoint: 50.0})
    await redis_client.hset(keys.lease_owners, endpoint, "old-worker")

    reclaimed = await redis_client.eval(
        RECLAIM_EXPIRED,
        3,
        keys.leased,
        keys.due,
        keys.lease_owners,
        60,
        10,
    )
    stale_completion = await redis_client.eval(
        COMPLETE_LEASE,
        5,
        keys.records,
        keys.quality,
        keys.due,
        keys.leased,
        keys.lease_owners,
        endpoint,
        "old-worker",
        '{"status":"stale"}',
        0.9,
        100,
    )
    claimed = await redis_client.eval(
        CLAIM_DUE,
        3,
        keys.due,
        keys.leased,
        keys.lease_owners,
        60,
        10,
        122,
        "new-worker",
    )

    assert reclaimed == [endpoint]
    assert stale_completion == 0
    assert claimed == [endpoint]
    assert await redis_client.hget(keys.lease_owners, endpoint) == "new-worker"


@pytest.mark.docker
async def test_repository_instances_cannot_share_one_active_lease(
    redis_client: Redis,
) -> None:
    first_repo = RedisRepository(redis_client, prefix="ippool:test")
    second_repo = RedisRepository(redis_client, prefix="ippool:test")
    record = make_record()
    await first_repo.upsert_candidate(
        record.model_copy(update={"next_check_at": datetime.fromtimestamp(1, UTC)})
    )

    first, second = await asyncio.gather(
        first_repo.claim_due("example.com", "worker-1", 1, 60, now=2),
        second_repo.claim_due("example.com", "worker-2", 1, 60, now=2),
    )
    leases = first + second

    assert len(leases) == 1
    winner = leases[0]
    stale = Lease(
        domain=winner.domain,
        endpoint=winner.endpoint,
        owner="stale-owner",
        expires_at=winner.expires_at,
    )
    assert await second_repo.complete(stale, record) is False
    assert await first_repo.complete(winner, record) is True


@pytest.mark.docker
async def test_repository_queries_and_stats_remain_bounded(
    redis_client: Redis,
) -> None:
    repo = RedisRepository(redis_client, prefix="ippool:test")
    for number in range(25):
        await repo.upsert_verified(make_record(f"1.1.1.{number + 1}:80"))

    page = await repo.list_proxies("example.com", min_score=80, limit=7, offset=0)
    selected = await repo.random_proxies("example.com", min_score=80, count=100)
    stats = await repo.stats("example.com")

    assert len(page.items) == 7
    assert page.next_offset == 7
    assert len(selected) <= 20
    assert stats.total == 25
    assert stats.available == 25
