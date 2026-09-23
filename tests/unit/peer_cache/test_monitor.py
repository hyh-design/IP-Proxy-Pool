from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

from ip_proxy_pool.peer_cache.monitor import PeerMonitor
from ip_proxy_pool.peer_cache.store import PeerCacheStore


class BrokenRedis:
    async def hgetall(self, _key: str) -> dict[str, str]:
        raise ConnectionError("redis unavailable")


class Notifier:
    def __init__(self) -> None:
        self.events = []

    async def send(self, event) -> bool:
        self.events.append((event.event_type, event.status, event.event_id))
        return True


class FlakyNotifier(Notifier):
    def __init__(self) -> None:
        super().__init__()
        self.results = [False, True]

    async def send(self, event) -> bool:
        self.events.append((event.event_type, event.status, event.event_id))
        return self.results.pop(0) if self.results else True


async def test_sync_failure_recovery_is_independent_of_low_inventory(isolated_redis: str) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    try:
        now = datetime.now(UTC)
        store = PeerCacheStore(
            redis, prefix="test", peer_name="system-two", domain="portal.daqihui.com"
        )
        notifier = Notifier()
        monitor = PeerMonitor(
            redis,
            store=store,
            notifier=notifier,
            prefix="test",
            peer_name="system-two",
            domain="portal.daqihui.com",
        )
        await redis.hset(
            store._keys.state,
            mapping={
                "monitor_started_at": str((now - timedelta(seconds=200)).timestamp()),
                "heartbeat_at": str(now.timestamp()),
                "consecutive_failures": "2",
            },
        )
        await monitor.tick(now)
        assert ("sync_failure", "failure") not in [(a, b) for a, b, _ in notifier.events]
        await redis.hset(store._keys.state, "consecutive_failures", "3")
        await monitor.tick(now + timedelta(seconds=16))
        assert [(a, b) for a, b, _ in notifier.events if a == "sync_failure"] == [
            ("sync_failure", "failure")
        ]
        await redis.hset(
            store._keys.state,
            mapping={
                "consecutive_failures": "0",
                "last_success_at": str((now + timedelta(seconds=30)).timestamp()),
                "heartbeat_at": str((now + timedelta(seconds=30)).timestamp()),
            },
        )
        await monitor.tick(now + timedelta(seconds=32))
        assert [(a, b) for a, b, _ in notifier.events if a == "sync_failure"] == [
            ("sync_failure", "failure"),
            ("sync_failure", "recovery"),
        ]
        assert ("cache_low", "recovery") not in [(a, b) for a, b, _ in notifier.events]
    finally:
        await redis.aclose()


async def test_monitor_persists_retry_and_second_instance_does_not_duplicate(
    isolated_redis: str,
) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    try:
        now = datetime.now(UTC)
        store = PeerCacheStore(
            redis, prefix="test", peer_name="system-two", domain="portal.daqihui.com"
        )
        await redis.hset(
            store._keys.state,
            mapping={
                "monitor_started_at": str(now.timestamp()),
                "heartbeat_at": str(now.timestamp()),
                "consecutive_failures": "3",
            },
        )
        notifier = FlakyNotifier()
        first = PeerMonitor(
            redis,
            store=store,
            notifier=notifier,
            prefix="test",
            peer_name="system-two",
            domain="portal.daqihui.com",
        )
        second = PeerMonitor(
            redis,
            store=store,
            notifier=notifier,
            prefix="test",
            peer_name="system-two",
            domain="portal.daqihui.com",
        )
        await first.tick(now)
        await second.tick(now + timedelta(seconds=15))
        assert [(a, b) for a, b, _ in notifier.events if a == "sync_failure"] == [
            ("sync_failure", "failure")
        ]
        await second.tick(now + timedelta(seconds=61))
        failures = [(a, b, event_id) for a, b, event_id in notifier.events if a == "sync_failure"]
        assert len(failures) == 2
        assert failures[0][2] == failures[1][2]
        await first.tick(now + timedelta(seconds=76))
        assert len([event for event in notifier.events if event[0] == "sync_failure"]) == 2
    finally:
        await redis.aclose()


async def test_metrics_report_unavailable_when_redis_cannot_be_read() -> None:
    redis = BrokenRedis()
    store = PeerCacheStore(
        redis, prefix="test", peer_name="system-two", domain="portal.daqihui.com"
    )
    monitor = PeerMonitor(
        redis,
        store=store,
        notifier=Notifier(),
        prefix="test",
        peer_name="system-two",
        domain="portal.daqihui.com",
    )
    snapshot = await monitor.metrics_snapshot(datetime.now(UTC))
    assert snapshot.available is False
    assert snapshot.valid_count is None
    assert await monitor.tick(datetime.now(UTC)) is False
