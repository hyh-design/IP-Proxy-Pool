from datetime import UTC, datetime, timedelta

import fakeredis.aioredis

from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState
from ip_proxy_pool.storage.codec import encode_record
from ip_proxy_pool.storage.keys import keys_for
from ip_proxy_pool.storage.repository import RedisRepository


def record(
    address: str,
    *,
    score: int,
    state: ProxyState,
    age_minutes: int,
) -> ProxyRecord:
    now = datetime.now(UTC)
    seen = now - timedelta(minutes=age_minutes)
    return ProxyRecord(
        endpoint=ProxyEndpoint.parse(address),
        domain="example.com",
        score=score,
        state=state,
        source_names={"capacity-test"},
        first_seen_at=seen,
        last_seen_at=seen,
        next_check_at=now,
    )


async def seed_record(client: fakeredis.aioredis.FakeRedis, item: ProxyRecord) -> None:
    keys = keys_for("ippool:test", item.domain)
    endpoint = item.endpoint.canonical
    await client.hset(keys.records, endpoint, encode_record(item))
    await client.zadd(keys.quality, {endpoint: item.score})
    await client.zadd(keys.due, {endpoint: item.next_check_at.timestamp()})


async def test_capacity_never_evicts_active_lease() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repo = RedisRepository(client, prefix="ippool:test")
    try:
        leased = record("1.1.1.1:80", score=1, state=ProxyState.QUARANTINED, age_minutes=30)
        await seed_record(client, leased)
        await seed_record(
            client,
            record(
                "8.8.8.8:80",
                score=10,
                state=ProxyState.QUARANTINED,
                age_minutes=20,
            ),
        )
        await seed_record(
            client, record("9.9.9.9:80", score=20, state=ProxyState.CANDIDATE, age_minutes=10)
        )
        keys = keys_for("ippool:test", "example.com")
        await client.zadd(keys.leased, {leased.endpoint.canonical: 999.0})
        await client.hset(keys.lease_owners, leased.endpoint.canonical, "worker")

        removed = await repo.enforce_capacity("example.com", maximum=1)

        assert removed == 2
        assert await repo.get_record("example.com", leased.endpoint.canonical) is not None
        stats = await repo.stats("example.com")
        assert stats.total == 1
        assert stats.quarantined == 1
        assert stats.leased == 1
    finally:
        await client.aclose()


async def test_capacity_prefers_quarantined_then_low_score_oldest() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    repo = RedisRepository(client, prefix="ippool:test")
    try:
        quarantined = record("1.1.1.1:80", score=99, state=ProxyState.QUARANTINED, age_minutes=5)
        low_old = record("8.8.8.8:80", score=10, state=ProxyState.CANDIDATE, age_minutes=20)
        low_new = record("9.9.9.9:80", score=10, state=ProxyState.CANDIDATE, age_minutes=10)
        for item in (quarantined, low_old, low_new):
            await seed_record(client, item)

        removed = await repo.enforce_capacity("example.com", maximum=1)

        assert removed == 2
        assert await repo.get_record("example.com", quarantined.endpoint.canonical) is None
        assert await repo.get_record("example.com", low_old.endpoint.canonical) is None
        assert await repo.get_record("example.com", low_new.endpoint.canonical) is not None
    finally:
        await client.aclose()
