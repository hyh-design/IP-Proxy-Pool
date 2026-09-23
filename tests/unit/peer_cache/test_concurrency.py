from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

from ip_proxy_pool.peer_cache.policy import SelectionPolicy
from ip_proxy_pool.peer_cache.receipts import SelectionReceiptStore
from ip_proxy_pool.peer_cache.store import PeerCacheStore

from .test_store import item


async def test_failed_feedback_blocks_old_snapshot_and_is_idempotent(isolated_redis: str) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    try:
        now = datetime.now(UTC)
        policy = SelectionPolicy("portal.daqihui.com", 90, 2000, 600, 2)
        store = PeerCacheStore(redis, prefix="test", peer_name="system-two", domain=policy.domain)
        receipts = SelectionReceiptStore(
            redis, prefix="test", domain=policy.domain, peer_name="system-two"
        )
        first = await store.begin_sync("worker-one")
        await store.replace((item(now),), first, "worker-one", now)
        selected = (await store.select(policy, 1, (), "fingerprint", now))[0]
        receipt = await receipts.resolve(
            selected.selection_token, "fingerprint", policy.domain, selected.endpoint, now
        )
        assert await store.invalidate(receipt, now) is True
        assert await store.invalidate(receipt, now) is False
        assert await store.count(policy, now) == 0
        second = await store.begin_sync("worker-two")
        result = await store.replace((item(now),), second, "worker-two", now)
        assert result.suppressed == 1
        assert await store.count(policy, now) == 0
        later = now + timedelta(seconds=601)
        third = await store.begin_sync("worker-three")
        result = await store.replace((item(later, checked_at=now),), third, "worker-three", later)
        assert result.accepted == 0
        fourth = await store.begin_sync("worker-four")
        result = await store.replace(
            (item(later, checked_at=now + timedelta(seconds=7)),), fourth, "worker-four", later
        )
        assert result.accepted == 1
        cleanup_at = now + timedelta(seconds=1206)
        fifth = await store.begin_sync("worker-five")
        assert fifth is not None
        await store.replace((item(cleanup_at),), fifth, "worker-five", cleanup_at)
        assert await redis.hlen(store._keys.suppression) == 0
    finally:
        await redis.aclose()


async def test_stale_generation_cannot_overwrite_newer_snapshot(isolated_redis: str) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    try:
        now = datetime.now(UTC)
        store = PeerCacheStore(
            redis, prefix="test", peer_name="system-two", domain="portal.daqihui.com"
        )
        old = await store.begin_sync("worker-old")
        await redis.delete(store._keys.lock)
        new = await store.begin_sync("worker-new")
        assert old is not None and new is not None and new > old
        fresh = item(now, endpoint="8.8.8.8:80")
        accepted = await store.replace((fresh,), new, "worker-new", now)
        assert accepted.accepted == 1
        stale = await store.replace((item(now),), old, "worker-old", now)
        assert stale.stale_generation is True
        policy = SelectionPolicy("portal.daqihui.com", 90, 2000, 600, 2)
        selected = await store.select(policy, 20, (), "fingerprint", now)
        assert [value.endpoint for value in selected] == ["8.8.8.8:80"]
    finally:
        await redis.aclose()
