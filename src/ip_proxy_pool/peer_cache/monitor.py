"""Redis-led, persisted peer health event state machine."""

import asyncio
import json
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from ip_proxy_pool.peer_cache.alerts import PeerAlertEvent, PeerAlertNotifier
from ip_proxy_pool.peer_cache.policy import SelectionPolicy
from ip_proxy_pool.peer_cache.store import PeerCacheStore
from ip_proxy_pool.storage.keys import peer_cache_keys_for

EVENT_TYPES = ("sync_failure", "cache_low", "cache_evictions", "worker_stale", "cache_store_error")
_RELEASE_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


@dataclass(slots=True)
class EventState:
    active: bool = False
    notified: bool = False
    pending: str | None = None
    event_id: str = ""
    started_at: float = 0.0
    retry_at: float = 0.0
    attempts: int = 0
    last_sent_at: float = 0.0


@dataclass(frozen=True, slots=True)
class PeerMetricsSnapshot:
    available: bool
    valid_count: int | None
    consecutive_failures: int | None
    heartbeat_at: float | None
    last_success_at: float | None
    evictions_10m: int | None


class PeerMonitor:
    def __init__(
        self,
        redis: Any,
        *,
        store: PeerCacheStore,
        notifier: PeerAlertNotifier,
        prefix: str,
        peer_name: str,
        domain: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.redis = redis
        self.store = store
        self.notifier = notifier
        self.keys = peer_cache_keys_for(prefix, domain, peer_name)
        self.peer_name = peer_name
        self.domain = domain
        self.now = now
        self.lock_key = f"{self.keys.state}:monitor-lock"

    async def metrics_snapshot(self, now: datetime) -> PeerMetricsSnapshot:
        try:
            state = await self.redis.hgetall(self.keys.state)
            count = await self.store.count(SelectionPolicy(self.domain, 90, 2000, 600, 2), now)
            evictions = int(
                await self.redis.zcount(self.keys.evictions, now.timestamp() - 600, "+inf")
            )
            return PeerMetricsSnapshot(
                available=True,
                valid_count=count,
                consecutive_failures=int(state.get("consecutive_failures", "0")),
                heartbeat_at=float(state.get("heartbeat_at", "0")),
                last_success_at=float(state.get("last_success_at", "0")),
                evictions_10m=evictions,
            )
        except Exception:
            return PeerMetricsSnapshot(False, None, None, None, None, None)

    async def tick(self, now: datetime) -> bool:
        owner = uuid4().hex
        try:
            locked = await self.redis.set(self.lock_key, owner, nx=True, ex=15)
        except Exception:
            return False
        if not locked:
            return False
        try:
            await self._tick_locked(now)
            return True
        except Exception:
            return False
        finally:
            with suppress(Exception):
                await self.redis.eval(_RELEASE_LOCK, 1, self.lock_key, owner)

    async def _tick_locked(self, now: datetime) -> None:
        epoch = now.timestamp()
        await self.redis.hsetnx(self.keys.state, "monitor_started_at", str(epoch))
        state = await self.redis.hgetall(self.keys.state)
        started_at = float(state.get("monitor_started_at", epoch))
        grace_done = epoch - started_at > 180
        store_errors = int(state.get("monitor_store_failures", "0"))
        count: int | None = None
        try:
            count = await self.store.count(SelectionPolicy(self.domain, 90, 2000, 600, 2), now)
            await self.redis.hset(self.keys.state, "monitor_store_failures", 0)
            store_errors = 0
        except Exception:
            store_errors = int(
                await self.redis.hincrby(self.keys.state, "monitor_store_failures", 1)
            )
        evictions = int(await self.redis.zcount(self.keys.evictions, epoch - 600, "+inf"))
        last_success = float(state.get("last_success_at", "0"))
        heartbeat = float(state.get("heartbeat_at", "0"))
        conditions = {
            "sync_failure": int(state.get("consecutive_failures", "0")) >= 3,
            "cache_low": grace_done and (count is not None and count < 5),
            "cache_evictions": evictions >= 3,
            "worker_stale": grace_done and (heartbeat == 0 or epoch - heartbeat > 180),
            "cache_store_error": max(store_errors, int(state.get("api_store_failures", "0"))) >= 3,
        }
        for event_type in EVENT_TYPES:
            await self._transition(
                event_type,
                conditions[event_type],
                now,
                last_success=last_success,
                valid_count=count,
            )

    async def _transition(
        self,
        event_type: str,
        active: bool,
        now: datetime,
        *,
        last_success: float,
        valid_count: int | None,
    ) -> None:
        key = f"{self.keys.state}:event:{event_type}"
        raw = await self.redis.get(key)
        state = EventState(**json.loads(raw)) if raw else EventState()
        epoch = now.timestamp()
        if active and not state.active and state.notified:
            state.active = True
            state.pending = None
            state.retry_at = 0
            state.attempts = 0
        elif active and not state.active:
            state.active = True
            state.pending = "failure"
            state.event_id = uuid4().hex
            state.started_at = epoch
            state.retry_at = 0
            state.attempts = 0
        elif not active and state.active:
            recovery_allowed = True
            if event_type in {"sync_failure", "worker_stale"}:
                recovery_allowed = last_success > state.started_at
            elif event_type in {"cache_low", "cache_evictions"}:
                recovery_allowed = (
                    last_success > state.started_at and valid_count is not None and valid_count >= 5
                )
            if not recovery_allowed:
                await self.redis.set(key, json.dumps(asdict(state), separators=(",", ":")))
                return
            state.active = False
            state.pending = "recovery" if state.notified else None
            state.retry_at = 0
            state.attempts = 0
        if state.pending is not None and epoch >= state.retry_at:
            event = PeerAlertEvent(
                state.event_id, self.peer_name, self.domain, event_type, state.pending
            )
            try:
                sent = await self.notifier.send(event)
            except Exception:
                sent = False
            if sent:
                state.notified = state.pending == "failure"
                state.pending = None
                state.last_sent_at = epoch
                state.attempts = 0
            else:
                state.attempts += 1
                state.retry_at = epoch + (60, 120, 300)[min(state.attempts - 1, 2)]
        await self.redis.set(key, json.dumps(asdict(state), separators=(",", ":")))

    async def run(self, stop_event: asyncio.Event, *, interval_seconds: int = 15) -> None:
        try:
            while not stop_event.is_set():
                await self.tick(self.now())
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                except TimeoutError:
                    continue
        finally:
            await self.notifier.aclose()
