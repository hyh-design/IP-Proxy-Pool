"""Isolated peer cache backed by atomic Redis scripts."""

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from ip_proxy_pool.peer_cache.lua import ABORT_SYNC, BEGIN_SYNC, INVALIDATE, REPLACE, SELECT
from ip_proxy_pool.peer_cache.models import PeerCacheRecord
from ip_proxy_pool.peer_cache.policy import SelectionPolicy, accepts, effective_peer_policy
from ip_proxy_pool.peer_cache.receipts import SelectionReceipt, token_digest
from ip_proxy_pool.storage.keys import PeerCacheKeys, keys_for, peer_cache_keys_for


@dataclass(frozen=True, slots=True)
class ReplaceResult:
    accepted: int
    filtered: int
    suppressed: int
    stale_generation: bool


@dataclass(frozen=True, slots=True)
class SelectedProxy:
    endpoint: str
    domain: str
    score: int
    latency_ewma_ms: float
    last_checked_at: datetime
    consecutive_successes: int
    source_names: tuple[str, ...]
    selection_source: str
    peer_name: str
    selection_token: str
    usable_until: datetime


class PeerCacheStore:
    def __init__(
        self,
        redis: Any,
        *,
        prefix: str,
        peer_name: str,
        domain: str,
        policy: SelectionPolicy | None = None,
        cache_ttl_seconds: int = 180,
        cooldown_seconds: int = 600,
        origin_node: str | None = None,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._peer_name = peer_name
        self._domain = domain
        self._keys: PeerCacheKeys = peer_cache_keys_for(prefix, domain, peer_name)
        self._formal_keys = keys_for(prefix, domain)
        configured_policy = policy or SelectionPolicy(domain, 90, 2000, 600, 2)
        self._policy = effective_peer_policy(
            configured_policy, configured_policy, configured_policy
        )
        if cache_ttl_seconds <= 0 or cooldown_seconds <= 0:
            raise ValueError("cache TTL and cooldown must be positive")
        self._cache_ttl = min(cache_ttl_seconds, 180)
        self._cooldown = max(cooldown_seconds, 600)
        self._origin_node = origin_node

    async def begin_sync(self, owner: str) -> int | None:
        if not owner:
            raise ValueError("sync owner required")
        result = await self._redis.eval(
            BEGIN_SYNC, 2, self._keys.generation, self._keys.lock, owner
        )
        return int(result) or None

    async def abort_sync(self, generation: int, owner: str) -> None:
        await self._redis.eval(ABORT_SYNC, 1, self._keys.lock, owner, str(generation))

    async def note_failure(self, outcome: str, now: datetime) -> None:
        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.hincrby(self._keys.state, "consecutive_failures", 1)
            pipeline.hset(
                self._keys.state,
                mapping={
                    "last_attempt_at": str(now.timestamp()),
                    "heartbeat_at": str(now.timestamp()),
                    "last_outcome": outcome,
                },
            )
            await pipeline.execute()

    async def heartbeat(self, now: datetime) -> None:
        await self._redis.hset(self._keys.state, "heartbeat_at", str(now.timestamp()))

    async def note_api_result(self, *, success: bool) -> None:
        if success:
            await self._redis.hset(self._keys.state, "api_store_failures", 0)
        else:
            await self._redis.hincrby(self._keys.state, "api_store_failures", 1)

    async def replace(
        self,
        snapshot: tuple[PeerCacheRecord, ...],
        generation: int | None,
        lock_owner: str,
        now: datetime,
    ) -> ReplaceResult:
        if generation is None or generation <= 0 or not lock_owner:
            raise ValueError("valid sync generation and owner required")
        if len(snapshot) > 20:
            raise ValueError("peer snapshot exceeds 20 items")
        if len({item.endpoint for item in snapshot}) != len(snapshot):
            raise ValueError("duplicate peer endpoint")
        current = now.timestamp()
        encoded: list[str] = []
        filtered = 0
        for item in snapshot:
            if (
                item.domain != self._domain
                or item.peer_name != self._peer_name
                or (self._origin_node is not None and item.origin_node != self._origin_node)
            ):
                raise ValueError("peer snapshot identity mismatch")
            if (item.synced_at - now).total_seconds() > 5:
                raise ValueError("peer snapshot sync time is in the future")
            if not accepts(item, self._policy, now):
                filtered += 1
                continue
            expires_at = min(
                item.expires_at.timestamp(),
                item.synced_at.timestamp() + self._cache_ttl,
                item.last_checked_at.timestamp() + self._policy.max_checked_age_seconds - 5,
            )
            if expires_at <= current:
                filtered += 1
                continue
            value = item.model_dump(mode="json")
            value["checked_epoch"] = item.last_checked_at.timestamp()
            value["expires_epoch"] = expires_at
            encoded.append(json.dumps(value, separators=(",", ":"), allow_nan=False))
        result = cast(
            list[int],
            await self._redis.eval(
                REPLACE,
                8,
                self._keys.records,
                self._keys.expiry,
                self._keys.suppression,
                self._keys.generation,
                self._keys.lock,
                self._formal_keys.records,
                self._keys.state,
                self._keys.suppression_expiry,
                str(generation),
                lock_owner,
                str(current),
                *encoded,
            ),
        )
        return ReplaceResult(
            accepted=int(result[0]),
            filtered=filtered + int(result[1]),
            suppressed=int(result[2]),
            stale_generation=bool(result[3]),
        )

    async def select(
        self,
        policy: SelectionPolicy,
        count: int,
        exclusions: tuple[str, ...],
        principal_fingerprint: str,
        now: datetime,
    ) -> tuple[SelectedProxy, ...]:
        if policy.domain != self._domain or not 1 <= count <= 20:
            raise ValueError("invalid peer selection")
        policy = effective_peer_policy(policy, self._policy, self._policy)
        tokens = [secrets.token_urlsafe(32) for _ in range(count)]
        pairs = [value for token in tokens for value in (token, token_digest(token))]
        raw = await self._redis.eval(
            SELECT,
            5,
            self._keys.records,
            self._keys.expiry,
            self._formal_keys.records,
            self._keys.suppression,
            self._keys.receipts_prefix,
            str(now.timestamp()),
            str(policy.min_score),
            str(policy.max_latency_ms),
            str(policy.max_checked_age_seconds),
            str(policy.min_consecutive_successes),
            str(count),
            principal_fingerprint,
            "select",
            json.dumps(exclusions),
            self._domain,
            *pairs,
        )
        return tuple(self._decode_selected(value) for value in raw)

    async def count(self, policy: SelectionPolicy, now: datetime) -> int:
        if policy.domain != self._domain:
            raise ValueError("peer policy domain mismatch")
        policy = effective_peer_policy(policy, self._policy, self._policy)
        raw = await self._redis.eval(
            SELECT,
            5,
            self._keys.records,
            self._keys.expiry,
            self._formal_keys.records,
            self._keys.suppression,
            self._keys.receipts_prefix,
            str(now.timestamp()),
            str(policy.min_score),
            str(policy.max_latency_ms),
            str(policy.max_checked_age_seconds),
            str(policy.min_consecutive_successes),
            "20",
            "",
            "count",
            "[]",
            self._domain,
        )
        return len(raw)

    async def invalidate(self, receipt: SelectionReceipt, now: datetime) -> bool:
        if receipt.selection_source != "peer" or receipt.peer_name != self._peer_name:
            raise ValueError("receipt is not for this peer cache")
        if receipt.domain != self._domain:
            raise ValueError("receipt domain mismatch")
        result = await self._redis.eval(
            INVALIDATE,
            6,
            self._keys.receipts_prefix + receipt.digest,
            self._keys.suppression,
            self._keys.records,
            self._keys.expiry,
            self._keys.suppression_expiry,
            self._keys.evictions,
            str(now.timestamp()),
            str(self._cooldown),
            str(max(self._cooldown, self._policy.max_checked_age_seconds) + 605),
        )
        if int(result) < 0:
            raise ValueError("selection receipt expired")
        return bool(result)

    @staticmethod
    def _decode_selected(raw: str) -> SelectedProxy:
        value = json.loads(raw)
        item = value["record"]
        return SelectedProxy(
            endpoint=item["endpoint"],
            domain=item["domain"],
            score=item["score"],
            latency_ewma_ms=item["latency_ewma_ms"],
            last_checked_at=datetime.fromtimestamp(item["checked_epoch"], UTC),
            consecutive_successes=item["consecutive_successes"],
            source_names=tuple(item["source_names"]),
            selection_source="peer",
            peer_name=item["peer_name"],
            selection_token=value["token"],
            usable_until=datetime.fromtimestamp(value["usable_until"], UTC),
        )
