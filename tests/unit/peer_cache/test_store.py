from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis

from ip_proxy_pool.peer_cache.models import PeerCacheRecord
from ip_proxy_pool.peer_cache.policy import SelectionPolicy
from ip_proxy_pool.peer_cache.store import PeerCacheStore
from ip_proxy_pool.storage.keys import keys_for


def item(
    now: datetime, *, endpoint: str = "1.1.1.1:80", checked_at: datetime | None = None
) -> PeerCacheRecord:
    return PeerCacheRecord(
        peer_name="system-two",
        origin_node="system-two",
        domain="portal.daqihui.com",
        endpoint=endpoint,
        score=95,
        latency_ewma_ms=100,
        last_checked_at=checked_at or now,
        consecutive_successes=3,
        source_names=("formal",),
        synced_at=now,
        expires_at=now + timedelta(seconds=180),
    )


async def test_replace_and_selection_do_not_touch_formal_pool(isolated_redis: str) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    try:
        store = PeerCacheStore(
            redis, prefix="test", peer_name="system-two", domain="portal.daqihui.com"
        )
        now = datetime.now(UTC)
        formal = keys_for("test", "portal.daqihui.com")
        await redis.hset(formal.records, "8.8.8.8:80", "formal")
        before = await redis.hgetall(formal.records)
        generation = await store.begin_sync("worker-one")
        result = await store.replace(
            (item(now), item(now, endpoint="8.8.8.8:80")), generation, "worker-one", now
        )
        assert result.accepted == 1
        policy = SelectionPolicy("portal.daqihui.com", 90, 2000, 600, 2)
        selected = await store.select(policy, 20, (), "fingerprint", now)
        assert [value.endpoint for value in selected] == ["1.1.1.1:80"]
        assert selected[0].selection_source == "peer"
        assert selected[0].selection_token
        assert await redis.hgetall(formal.records) == before
        assert (await store.count(policy, now)) == 1
    finally:
        await redis.aclose()


async def test_empty_snapshot_replaces_candidates_without_deleting_suppression(
    isolated_redis: str,
) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    try:
        store = PeerCacheStore(
            redis, prefix="test", peer_name="system-two", domain="portal.daqihui.com"
        )
        now = datetime.now(UTC)
        generation = await store.begin_sync("worker-one")
        await store.replace((item(now),), generation, "worker-one", now)
        generation = await store.begin_sync("worker-two")
        result = await store.replace((), generation, "worker-two", now)
        assert result.accepted == 0
        assert await store.count(SelectionPolicy("portal.daqihui.com", 90, 2000, 600, 2), now) == 0
    finally:
        await redis.aclose()


async def test_selection_rechecks_strict_request_and_expiry_boundary(isolated_redis: str) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    try:
        now = datetime.now(UTC)
        store = PeerCacheStore(
            redis, prefix="test", peer_name="system-two", domain="portal.daqihui.com"
        )
        generation = await store.begin_sync("worker")
        await store.replace((item(now),), generation, "worker", now)
        strict = SelectionPolicy("portal.daqihui.com", 95, 50, 60, 3)
        assert await store.count(strict, now) == 0
        qualified = SelectionPolicy("portal.daqihui.com", 95, 100, 60, 3)
        assert await store.count(qualified, now) == 1
        assert await store.count(qualified, now + timedelta(seconds=56)) == 0
        assert await store.count(qualified, now + timedelta(seconds=60)) == 0
        assert (
            await store.count(
                SelectionPolicy("portal.daqihui.com", 0, 60000, 86400, 1),
                now + timedelta(seconds=180),
            )
            == 0
        )
    finally:
        await redis.aclose()


async def test_mismatched_origin_rejects_entire_snapshot_without_replacing(
    isolated_redis: str,
) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    try:
        now = datetime.now(UTC)
        store = PeerCacheStore(
            redis,
            prefix="test",
            peer_name="system-two",
            domain="portal.daqihui.com",
            origin_node="system-two",
        )
        generation = await store.begin_sync("worker")
        with pytest.raises(ValueError, match="identity"):
            await store.replace(
                (item(now).model_copy(update={"origin_node": "impostor"}),),
                generation,
                "worker",
                now,
            )
        assert await store.count(SelectionPolicy("portal.daqihui.com", 90, 2000, 600, 2), now) == 0
    finally:
        await redis.aclose()


async def test_formal_record_added_after_import_vetoes_peer_selection(isolated_redis: str) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    try:
        now = datetime.now(UTC)
        domain = "portal.daqihui.com"
        store = PeerCacheStore(redis, prefix="test", peer_name="system-two", domain=domain)
        generation = await store.begin_sync("worker")
        await store.replace((item(now),), generation, "worker", now)
        await redis.hset(keys_for("test", domain).records, "1.1.1.1:80", "quarantined")
        policy = SelectionPolicy(domain, 90, 2000, 600, 2)
        assert await store.count(policy, now) == 0
        assert await store.select(policy, 1, (), "fingerprint", now) == ()
    finally:
        await redis.aclose()
