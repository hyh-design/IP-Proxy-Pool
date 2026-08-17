from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest

from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState
from ip_proxy_pool.storage.codec import encode_record
from ip_proxy_pool.storage.keys import keys_for
from ip_proxy_pool.storage.repository import (
    RedisRepository,
    latency_index_score,
    priority_due_score,
)


@pytest.fixture
async def fake_redis() -> fakeredis.aioredis.FakeRedis:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def available_record() -> ProxyRecord:
    now = datetime.now(UTC)
    return ProxyRecord(
        endpoint=ProxyEndpoint.parse("1.1.1.1:80"),
        domain="example.com",
        score=90,
        state=ProxyState.AVAILABLE,
        source_names={"source-a"},
        first_seen_at=now,
        last_seen_at=now,
        last_checked_at=now,
        next_check_at=now + timedelta(minutes=5),
    )


@pytest.mark.parametrize(
    ("state", "latency", "expected"),
    [
        (ProxyState.AVAILABLE, 0.0, 0.0),
        (ProxyState.AVAILABLE, 1000.0, 1000.0),
        (ProxyState.CANDIDATE, 100.0, None),
        (ProxyState.DEGRADED, 100.0, None),
        (ProxyState.QUARANTINED, 100.0, None),
        (ProxyState.AVAILABLE, None, None),
        (ProxyState.AVAILABLE, -1.0, None),
        (ProxyState.AVAILABLE, float("inf"), None),
        (ProxyState.AVAILABLE, float("nan"), None),
    ],
)
def test_latency_index_score_accepts_only_available_finite_non_negative_records(
    available_record: ProxyRecord,
    state: ProxyState,
    latency: float | None,
    expected: float | None,
) -> None:
    record = available_record.model_copy(update={"state": state, "latency_ewma_ms": latency})

    result = latency_index_score(record)

    assert result == expected


def test_priority_due_score_requires_selectable_latency(
    available_record: ProxyRecord,
) -> None:
    selectable = available_record.model_copy(update={"latency_ewma_ms": 999.9})
    assert priority_due_score(selectable, 1000) == selectable.next_check_at.timestamp()
    assert (
        priority_due_score(
            selectable.model_copy(update={"latency_ewma_ms": 1000.1}),
            1000,
        )
        is None
    )
    assert (
        priority_due_score(selectable.model_copy(update={"state": ProxyState.DEGRADED}), 1000)
        is None
    )


async def test_verified_upsert_is_immediately_available(
    fake_redis: fakeredis.aioredis.FakeRedis,
    available_record: ProxyRecord,
) -> None:
    repo = RedisRepository(fake_redis, prefix="ippool:test")

    await repo.upsert_verified(available_record)
    page = await repo.list_proxies(domain="example.com", min_score=80, limit=10, offset=0)

    assert [item.endpoint.canonical for item in page.items] == ["1.1.1.1:80"]
    assert page.next_offset is None
    assert await repo.list_domains() == ["example.com"]


async def test_verified_upsert_enforces_available_score_floor(
    fake_redis: fakeredis.aioredis.FakeRedis,
    available_record: ProxyRecord,
) -> None:
    repo = RedisRepository(fake_redis, prefix="ippool:test")
    weak_record = available_record.model_copy(update={"score": 25, "state": ProxyState.QUARANTINED})

    await repo.upsert_verified(weak_record)

    stored = await repo.get_record("example.com", "1.1.1.1:80")
    assert stored is not None
    assert stored.score == 80
    assert stored.state is ProxyState.AVAILABLE


async def test_candidate_upsert_merges_sources_without_downgrading(
    fake_redis: fakeredis.aioredis.FakeRedis,
    available_record: ProxyRecord,
) -> None:
    repo = RedisRepository(fake_redis, prefix="ippool:test")
    await repo.upsert_verified(available_record)
    later = available_record.last_seen_at + timedelta(minutes=1)
    candidate = available_record.model_copy(
        update={
            "score": 20,
            "state": ProxyState.CANDIDATE,
            "source_names": {"source-b"},
            "last_seen_at": later,
        }
    )

    await repo.upsert_candidate(candidate)

    stored = await repo.get_record("example.com", "1.1.1.1:80")
    assert stored is not None
    assert stored.score == 90
    assert stored.state is ProxyState.AVAILABLE
    assert stored.source_names == {"source-a", "source-b"}
    assert stored.last_seen_at == later


async def test_save_record_keeps_available_latency_index_in_sync(
    fake_redis: fakeredis.aioredis.FakeRedis,
    available_record: ProxyRecord,
) -> None:
    repo = RedisRepository(fake_redis, prefix="ippool:test")
    keys = keys_for("ippool:test", "example.com")
    endpoint = available_record.endpoint.canonical

    await repo.save_record(available_record.model_copy(update={"latency_ewma_ms": 125.0}))
    assert await fake_redis.zscore(keys.available_latency, endpoint) == 125.0
    assert await fake_redis.zscore(keys.priority_due, endpoint) == pytest.approx(
        available_record.next_check_at.timestamp()
    )

    await repo.save_record(available_record.model_copy(update={"latency_ewma_ms": 240.0}))
    assert await fake_redis.zscore(keys.available_latency, endpoint) == 240.0

    await repo.save_record(
        available_record.model_copy(
            update={"state": ProxyState.QUARANTINED, "latency_ewma_ms": 240.0}
        )
    )
    assert await fake_redis.zscore(keys.available_latency, endpoint) is None
    assert await fake_redis.zscore(keys.priority_due, endpoint) is None


async def test_new_candidate_is_not_added_to_available_latency_index(
    fake_redis: fakeredis.aioredis.FakeRedis,
    available_record: ProxyRecord,
) -> None:
    repo = RedisRepository(fake_redis, prefix="ippool:test")
    keys = keys_for("ippool:test", "example.com")
    candidate = available_record.model_copy(
        update={"state": ProxyState.CANDIDATE, "latency_ewma_ms": 100.0}
    )
    await fake_redis.zadd(keys.available_latency, {candidate.endpoint.canonical: 100.0})

    await repo.upsert_candidate(candidate)

    assert await fake_redis.zscore(keys.available_latency, candidate.endpoint.canonical) is None


async def test_complete_and_delete_leased_update_available_latency_atomically(
    fake_redis: fakeredis.aioredis.FakeRedis,
    available_record: ProxyRecord,
) -> None:
    repo = RedisRepository(fake_redis, prefix="ippool:test")
    keys = keys_for("ippool:test", "example.com")
    due = available_record.model_copy(
        update={"next_check_at": datetime.fromtimestamp(1, UTC), "latency_ewma_ms": None}
    )
    await repo.upsert_candidate(due)
    lease = (await repo.claim_due("example.com", "worker", 1, 60, now=2))[0]
    checked = due.model_copy(
        update={
            "state": ProxyState.AVAILABLE,
            "latency_ewma_ms": 150.0,
            "next_check_at": datetime.fromtimestamp(100, UTC),
        }
    )

    assert await repo.complete(lease, checked) is True
    assert await fake_redis.zscore(keys.available_latency, lease.endpoint) == 150.0

    next_lease = (await repo.claim_due("example.com", "worker", 1, 60, now=101))[0]
    assert await repo.delete_leased(next_lease) is True
    assert await fake_redis.zscore(keys.available_latency, lease.endpoint) is None


async def test_claim_due_prioritizes_fast_records_without_starving_general_queue(
    fake_redis: fakeredis.aioredis.FakeRedis,
    available_record: ProxyRecord,
) -> None:
    repo = RedisRepository(fake_redis, prefix="ippool:test", priority_max_latency_ms=1000)
    due_at = datetime.fromtimestamp(1, UTC)
    for address in ("1.1.1.1:80", "2.2.2.2:80", "3.3.3.3:80"):
        await repo.upsert_candidate(
            available_record.model_copy(
                update={
                    "endpoint": ProxyEndpoint.parse(address),
                    "state": ProxyState.CANDIDATE,
                    "next_check_at": due_at,
                }
            )
        )
    for address in ("8.8.8.8:80", "9.9.9.9:80", "10.10.10.10:80"):
        await repo.save_record(
            available_record.model_copy(
                update={
                    "endpoint": ProxyEndpoint.parse(address),
                    "latency_ewma_ms": 100.0,
                    "next_check_at": due_at,
                }
            )
        )

    leases = await repo.claim_due("example.com", "worker", 4, 60, now=2)
    endpoints = [lease.endpoint for lease in leases]

    assert len(set(endpoints[:2]) & {"8.8.8.8:80", "9.9.9.9:80", "10.10.10.10:80"}) == 2
    assert len(set(endpoints)) == 4
    assert len(set(endpoints) & {"1.1.1.1:80", "2.2.2.2:80", "3.3.3.3:80"}) == 2


async def test_list_limit_is_bounded(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    repo = RedisRepository(fake_redis, prefix="ippool:test")

    with pytest.raises(ValueError, match=r"1\.\.200"):
        await repo.list_proxies(domain="example.com", min_score=0, limit=201, offset=0)


async def test_list_filters_by_score_range_and_source(
    fake_redis: fakeredis.aioredis.FakeRedis,
    available_record: ProxyRecord,
) -> None:
    repo = RedisRepository(fake_redis, prefix="ippool:test")
    await repo.save_record(available_record.model_copy(update={"score": 95}))
    await repo.save_record(
        available_record.model_copy(
            update={
                "endpoint": ProxyEndpoint.parse("2.2.2.2:80"),
                "score": 96,
                "source_names": {"source-b"},
            }
        )
    )

    page = await repo.list_proxies(
        domain="example.com",
        min_score=80,
        max_score=95,
        source="source-a",
        limit=10,
        offset=0,
    )

    assert [item.endpoint.canonical for item in page.items] == ["1.1.1.1:80"]


async def test_filtered_pagination_tracks_underlying_scanned_offset(
    fake_redis: fakeredis.aioredis.FakeRedis,
    available_record: ProxyRecord,
) -> None:
    repo = RedisRepository(fake_redis, prefix="ippool:test")
    for index, (score, source) in enumerate(
        [(100, "source-b"), (99, "source-b"), (98, "source-a"), (97, "source-b"), (96, "source-a")],
        start=1,
    ):
        await repo.save_record(
            available_record.model_copy(
                update={
                    "endpoint": ProxyEndpoint.parse(f"10.0.0.{index}:80"),
                    "score": score,
                    "source_names": {source},
                }
            )
        )

    first = await repo.list_proxies(
        "example.com", min_score=0, limit=1, offset=0, source="source-a"
    )
    assert [item.score for item in first.items] == [98]
    assert first.next_offset == 3

    second = await repo.list_proxies(
        "example.com",
        min_score=0,
        limit=1,
        offset=first.next_offset,
        source="source-a",
    )
    assert [item.score for item in second.items] == [96]
    assert second.next_offset is None


async def test_record_scan_stops_at_limit_and_reports_remaining_records(
    fake_redis: fakeredis.aioredis.FakeRedis,
    available_record: ProxyRecord,
) -> None:
    repo = RedisRepository(fake_redis, prefix="ippool:test")
    for number in range(3):
        await repo.upsert_verified(
            available_record.model_copy(
                update={"endpoint": ProxyEndpoint.parse(f"1.1.1.{number + 1}:80")}
            )
        )

    result = await repo.scan_records("example.com", limit=2, batch_size=10)

    assert len(result.records) == 2
    assert result.scanned == 2
    assert result.partial is True


async def test_random_and_stats_are_bounded_and_typed(
    fake_redis: fakeredis.aioredis.FakeRedis,
    available_record: ProxyRecord,
) -> None:
    repo = RedisRepository(fake_redis, prefix="ippool:test")
    keys = keys_for("ippool:test", "example.com")
    for number in range(25):
        record = available_record.model_copy(
            update={
                "endpoint": ProxyEndpoint.parse(f"1.1.1.{number + 1}:80"),
                "state": ProxyState.AVAILABLE if number % 2 == 0 else ProxyState.DEGRADED,
                "latency_ewma_ms": float(number),
            }
        )
        await repo.upsert_verified(record)
    await fake_redis.set(keys.available_latency_ready, "1")

    selected = await repo.random_proxies("example.com", min_score=80, count=100, max_latency_ms=10)
    stats = await repo.stats("example.com")

    assert len(selected) <= 20
    assert len({item.endpoint.canonical for item in selected}) == len(selected)
    assert all((item.latency_ewma_ms or 0) <= 10 for item in selected)
    assert stats.total == 25
    assert stats.available == 25
    assert stats.due == 25


async def test_random_proxies_only_returns_recent_confirmed_hot_records(
    fake_redis: fakeredis.aioredis.FakeRedis,
    available_record: ProxyRecord,
) -> None:
    repo = RedisRepository(fake_redis, prefix="ippool:test")
    keys = keys_for("ippool:test", "example.com")
    now = datetime(2026, 8, 11, 8, tzinfo=UTC)
    base = available_record.model_copy(
        update={
            "score": 95,
            "state": ProxyState.AVAILABLE,
            "last_checked_at": now,
            "next_check_at": now + timedelta(minutes=5),
            "consecutive_successes": 2,
            "latency_ewma_ms": 1000.0,
        }
    )
    variants = [
        base,
        base.model_copy(
            update={
                "endpoint": ProxyEndpoint.parse("1.1.1.2:80"),
                "consecutive_successes": 1,
            }
        ),
        base.model_copy(
            update={
                "endpoint": ProxyEndpoint.parse("1.1.1.3:80"),
                "last_checked_at": now - timedelta(minutes=11),
            }
        ),
        base.model_copy(
            update={
                "endpoint": ProxyEndpoint.parse("1.1.1.4:80"),
                "latency_ewma_ms": 6000.0,
            }
        ),
        base.model_copy(
            update={
                "endpoint": ProxyEndpoint.parse("1.1.1.5:80"),
                "score": 85,
            }
        ),
        base.model_copy(
            update={
                "endpoint": ProxyEndpoint.parse("1.1.1.6:80"),
                "state": ProxyState.DEGRADED,
            }
        ),
    ]
    for record in variants:
        await repo.save_record(record)
    await fake_redis.set(keys.available_latency_ready, "1")

    selected = await repo.random_proxies(
        "example.com",
        min_score=90,
        count=20,
        max_latency_ms=5000,
        max_checked_age_seconds=600,
        min_consecutive_successes=2,
        now=now,
    )

    assert [record.endpoint.canonical for record in selected] == ["1.1.1.1:80"]


async def test_random_proxies_rejects_domain_without_built_latency_index(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    repo = RedisRepository(fake_redis, prefix="ippool:test")

    with pytest.raises(RuntimeError, match="latency index not ready"):
        await repo.random_proxies("example.com", min_score=80, count=1, max_latency_ms=1000)


async def test_random_proxies_returns_every_sparse_fast_candidate_reliably(
    fake_redis: fakeredis.aioredis.FakeRedis,
    available_record: ProxyRecord,
) -> None:
    repo = RedisRepository(fake_redis, prefix="ippool:test")
    keys = keys_for("ippool:test", "portal.daqihui.com")
    now = datetime(2026, 8, 17, 8, tzinfo=UTC)
    base = available_record.model_copy(
        update={
            "domain": "portal.daqihui.com",
            "score": 90,
            "state": ProxyState.AVAILABLE,
            "last_checked_at": now,
            "next_check_at": now + timedelta(minutes=5),
            "consecutive_successes": 2,
        }
    )
    pipeline = fake_redis.pipeline(transaction=False)
    fast_endpoints: set[str] = set()
    for number in range(10_000):
        endpoint = ProxyEndpoint.parse(
            f"10.{number // 65_536}.{(number // 256) % 256}.{number % 256}:80"
        )
        latency = 500.0 if number >= 9962 else 1500.0
        record = base.model_copy(update={"endpoint": endpoint, "latency_ewma_ms": latency})
        canonical = endpoint.canonical
        pipeline.hset(keys.records, canonical, encode_record(record))
        pipeline.zadd(keys.quality, {canonical: record.score})
        pipeline.zadd(keys.available_latency, {canonical: latency})
        if latency <= 1000:
            fast_endpoints.add(canonical)
    pipeline.sadd("ippool:test:domains", "portal.daqihui.com")
    pipeline.set(keys.available_latency_ready, "1")
    await pipeline.execute()

    seen: set[str] = set()
    for _ in range(20):
        selected = await repo.random_proxies(
            "portal.daqihui.com",
            min_score=80,
            count=20,
            max_latency_ms=1000,
            max_checked_age_seconds=600,
            min_consecutive_successes=2,
            now=now,
        )
        assert len(selected) == 20
        assert {item.endpoint.canonical for item in selected} <= fast_endpoints
        seen.update(item.endpoint.canonical for item in selected)

    assert len(seen) > 20

    retained = sorted(fast_endpoints)[:12]
    removed = sorted(fast_endpoints)[12:]
    await fake_redis.hdel(keys.records, *removed)
    await fake_redis.zrem(keys.available_latency, *removed)
    selected = await repo.random_proxies(
        "portal.daqihui.com",
        min_score=80,
        count=20,
        max_latency_ms=1000,
        max_checked_age_seconds=600,
        min_consecutive_successes=2,
        now=now,
    )
    assert {item.endpoint.canonical for item in selected} == set(retained)

    await fake_redis.hdel(keys.records, *retained)
    await fake_redis.zrem(keys.available_latency, *retained)
    assert (
        await repo.random_proxies("portal.daqihui.com", min_score=80, count=20, max_latency_ms=1000)
        == []
    )


async def test_random_proxies_self_heals_inconsistent_latency_members(
    fake_redis: fakeredis.aioredis.FakeRedis,
    available_record: ProxyRecord,
) -> None:
    repo = RedisRepository(fake_redis, prefix="ippool:test")
    keys = keys_for("ippool:test", "example.com")
    degraded = available_record.model_copy(
        update={"state": ProxyState.DEGRADED, "latency_ewma_ms": 100.0}
    )
    await fake_redis.hset(keys.records, degraded.endpoint.canonical, encode_record(degraded))
    await fake_redis.zadd(
        keys.available_latency,
        {degraded.endpoint.canonical: 100.0, "8.8.8.8:80": 200.0},
    )
    await fake_redis.set(keys.available_latency_ready, "1")

    assert (
        await repo.random_proxies("example.com", min_score=80, count=20, max_latency_ms=1000) == []
    )
    assert await fake_redis.zcard(keys.available_latency) == 0


async def test_selection_counts_apply_every_policy_constraint(
    fake_redis: fakeredis.aioredis.FakeRedis,
    available_record: ProxyRecord,
) -> None:
    repo = RedisRepository(fake_redis, prefix="ippool:test")
    keys = keys_for("ippool:test", "example.com")
    now = datetime(2026, 8, 17, 8, tzinfo=UTC)
    base = available_record.model_copy(
        update={
            "score": 95,
            "last_checked_at": now,
            "consecutive_successes": 2,
            "latency_ewma_ms": 500.0,
        }
    )
    variants = (
        base,
        base.model_copy(update={"endpoint": ProxyEndpoint.parse("1.1.1.2:80"), "score": 79}),
        base.model_copy(
            update={
                "endpoint": ProxyEndpoint.parse("1.1.1.3:80"),
                "last_checked_at": now - timedelta(minutes=11),
            }
        ),
        base.model_copy(
            update={
                "endpoint": ProxyEndpoint.parse("1.1.1.4:80"),
                "consecutive_successes": 1,
            }
        ),
        base.model_copy(
            update={
                "endpoint": ProxyEndpoint.parse("1.1.1.5:80"),
                "latency_ewma_ms": 1500.0,
            }
        ),
    )
    for record in variants:
        await repo.save_record(record)
    await fake_redis.set(keys.available_latency_ready, "1")

    counts = await repo.selection_counts(
        "example.com",
        min_score=80,
        max_latency_ms=1000,
        max_checked_age_seconds=600,
        min_consecutive_successes=2,
        now=now,
    )

    assert counts.indexed == 5
    assert counts.candidates == 4
    assert counts.selectable == 1
