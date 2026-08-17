from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import chain
from typing import Any, Generic, TypeVar, cast
from uuid import uuid4

from pydantic import BaseModel

from ip_proxy_pool.models import ProxyRecord, ProxyState
from ip_proxy_pool.storage.codec import decode_record, encode_record
from ip_proxy_pool.storage.keys import keys_for
from ip_proxy_pool.storage.lua import (
    CLAIM_DUE,
    COMPLETE_LEASE,
    DELETE_LEASE,
    RECLAIM_EXPIRED,
    RELEASE_LEASE,
)

T = TypeVar("T")


def latency_index_score(record: ProxyRecord) -> float | None:
    latency = record.latency_ewma_ms
    if record.state is not ProxyState.AVAILABLE or latency is None:
        return None
    value = float(latency)
    return value if math.isfinite(value) and value >= 0 else None


@dataclass(frozen=True, slots=True)
class Lease:
    domain: str
    endpoint: str
    owner: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    items: list[T]
    offset: int
    next_offset: int | None


@dataclass(frozen=True, slots=True)
class RecordScan:
    records: tuple[ProxyRecord, ...]
    scanned: int
    partial: bool


class PoolStats(BaseModel):
    total: int = 0
    candidate: int = 0
    available: int = 0
    degraded: int = 0
    quarantined: int = 0
    due: int = 0
    leased: int = 0


class RedisRepository:
    """Bounded, typed access to proxy-pool Redis data."""

    def __init__(self, redis: Any, *, prefix: str) -> None:
        if not prefix:
            raise ValueError("prefix must be non-empty")
        self._redis = redis
        self._prefix = prefix
        self._domains_key = f"{prefix}:domains"

    async def ping(self) -> bool:
        return bool(await self._redis.ping())

    async def close(self) -> None:
        await self._redis.aclose()

    async def list_domains(self) -> list[str]:
        domains = cast(set[str], await self._redis.smembers(self._domains_key))
        return sorted(domains)

    async def scan_records(
        self,
        domain: str,
        *,
        limit: int,
        batch_size: int = 500,
    ) -> RecordScan:
        if not 1 <= limit <= 100_000:
            raise ValueError("scan limit must be within 1..100000")
        if not 1 <= batch_size <= 1000:
            raise ValueError("scan batch size must be within 1..1000")
        key = keys_for(self._prefix, domain).records
        cursor = 0
        records: list[ProxyRecord] = []
        while True:
            cursor, raw_items = await self._redis.hscan(key, cursor=cursor, count=batch_size)
            payloads = list(cast(dict[str, str], raw_items).values())
            for index, payload in enumerate(payloads):
                records.append(decode_record(payload))
                if len(records) == limit:
                    remaining = index < len(payloads) - 1 or int(cursor) != 0
                    return RecordScan(tuple(records), len(records), remaining)
            if int(cursor) == 0:
                return RecordScan(tuple(records), len(records), False)

    async def get_record(self, domain: str, endpoint: str) -> ProxyRecord | None:
        keys = keys_for(self._prefix, domain)
        raw = cast(str | None, await self._redis.hget(keys.records, endpoint))
        return None if raw is None else decode_record(raw)

    async def record_count(self, domain: str) -> int:
        return int(await self._redis.hlen(keys_for(self._prefix, domain).records))

    async def is_due(self, domain: str, endpoint: str) -> bool:
        keys = keys_for(self._prefix, domain)
        score = await self._redis.zscore(keys.due, endpoint)
        return score is not None

    async def upsert_candidate(self, record: ProxyRecord) -> None:
        keys = keys_for(self._prefix, record.domain)
        endpoint = record.endpoint.canonical
        existing = await self.get_record(record.domain, endpoint)
        if existing is None:
            candidate = record.model_copy(update={"state": ProxyState.CANDIDATE})
            pipeline = self._redis.pipeline(transaction=True)
            pipeline.sadd(self._domains_key, record.domain)
            pipeline.hset(keys.records, endpoint, encode_record(candidate))
            pipeline.zadd(keys.quality, {endpoint: candidate.score})
            pipeline.zadd(keys.due, {endpoint: candidate.next_check_at.timestamp()})
            await pipeline.execute()
            return

        merged = existing.model_copy(
            update={
                "source_names": existing.source_names | record.source_names,
                "first_seen_at": min(existing.first_seen_at, record.first_seen_at),
                "last_seen_at": max(existing.last_seen_at, record.last_seen_at),
            }
        )
        pipeline = self._redis.pipeline(transaction=True)
        pipeline.sadd(self._domains_key, record.domain)
        pipeline.hset(keys.records, endpoint, encode_record(merged))
        await pipeline.execute()

    async def upsert_verified(self, record: ProxyRecord) -> None:
        verified = record.model_copy(
            update={
                "state": ProxyState.AVAILABLE,
                "score": max(80, record.score),
                "consecutive_successes": max(2, record.consecutive_successes),
            }
        )
        await self.save_record(verified)

    async def save_record(self, record: ProxyRecord) -> None:
        """Persist an already-scored record without changing its health state."""
        verified = record
        endpoint = verified.endpoint.canonical
        keys = keys_for(self._prefix, verified.domain)
        pipeline = self._redis.pipeline(transaction=True)
        pipeline.sadd(self._domains_key, verified.domain)
        pipeline.hset(keys.records, endpoint, encode_record(verified))
        pipeline.zadd(keys.quality, {endpoint: verified.score})
        pipeline.zadd(keys.due, {endpoint: verified.next_check_at.timestamp()})
        latency = latency_index_score(verified)
        if latency is None:
            pipeline.zrem(keys.available_latency, endpoint)
        else:
            pipeline.zadd(keys.available_latency, {endpoint: latency})
        await pipeline.execute()

    async def claim_due(
        self,
        domain: str,
        worker_id: str,
        limit: int,
        lease_seconds: int,
        now: float,
    ) -> list[Lease]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be within 1..1000")
        if not worker_id:
            raise ValueError("worker_id must be non-empty")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")

        keys = keys_for(self._prefix, domain)
        owner = f"{worker_id}:{uuid4().hex}"
        expires_at = now + lease_seconds
        members = cast(
            list[str],
            await self._redis.eval(
                CLAIM_DUE,
                3,
                keys.due,
                keys.leased,
                keys.lease_owners,
                str(now),
                str(limit),
                str(expires_at),
                owner,
            ),
        )
        return [
            Lease(
                domain=domain,
                endpoint=endpoint,
                owner=owner,
                expires_at=expires_at,
            )
            for endpoint in members
        ]

    async def complete(self, lease: Lease, record: ProxyRecord) -> bool:
        if record.domain != lease.domain:
            raise ValueError("record domain does not match lease")
        if record.endpoint.canonical != lease.endpoint:
            raise ValueError("record endpoint does not match lease")

        keys = keys_for(self._prefix, lease.domain)
        result = await self._redis.eval(
            COMPLETE_LEASE,
            6,
            keys.records,
            keys.quality,
            keys.due,
            keys.leased,
            keys.lease_owners,
            keys.available_latency,
            lease.endpoint,
            lease.owner,
            encode_record(record),
            str(record.score),
            str(record.next_check_at.timestamp()),
            "" if (latency := latency_index_score(record)) is None else str(latency),
        )
        return bool(result)

    async def release(self, lease: Lease, due_at: float) -> bool:
        keys = keys_for(self._prefix, lease.domain)
        result = await self._redis.eval(
            RELEASE_LEASE,
            3,
            keys.due,
            keys.leased,
            keys.lease_owners,
            lease.endpoint,
            lease.owner,
            str(due_at),
        )
        return bool(result)

    async def delete_leased(self, lease: Lease) -> bool:
        """Delete a terminally unhealthy proxy only if this worker owns its lease."""
        keys = keys_for(self._prefix, lease.domain)
        result = await self._redis.eval(
            DELETE_LEASE,
            6,
            keys.records,
            keys.quality,
            keys.due,
            keys.leased,
            keys.lease_owners,
            keys.available_latency,
            lease.endpoint,
            lease.owner,
        )
        return bool(result)

    async def reclaim_expired(self, domain: str, now: float, limit: int = 1000) -> list[str]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be within 1..1000")
        keys = keys_for(self._prefix, domain)
        return cast(
            list[str],
            await self._redis.eval(
                RECLAIM_EXPIRED,
                3,
                keys.leased,
                keys.due,
                keys.lease_owners,
                str(now),
                str(limit),
            ),
        )

    async def list_proxies(
        self,
        domain: str,
        min_score: int,
        limit: int,
        offset: int,
        state: ProxyState | None = None,
        max_score: int = 100,
        source: str | None = None,
    ) -> Page[ProxyRecord]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be within 1..200")
        if not 0 <= offset <= 10_000:
            raise ValueError("offset must be within 0..10000")
        if not 0 <= min_score <= 100:
            raise ValueError("min_score must be within 0..100")
        if not 0 <= max_score <= 100:
            raise ValueError("max_score must be within 0..100")
        if min_score > max_score:
            raise ValueError("min_score must not exceed max_score")
        if source is not None and not 1 <= len(source) <= 64:
            raise ValueError("source must be within 1..64 characters")

        keys = keys_for(self._prefix, domain)
        items: list[ProxyRecord] = []
        current = offset
        while current <= 10_000:
            fetch_count = min(max(limit * 5, 50), 1001, 10_001 - current)
            members = cast(
                list[str],
                await self._redis.zrevrangebyscore(
                    keys.quality,
                    max_score,
                    min_score,
                    start=current,
                    num=fetch_count,
                ),
            )
            if not members:
                return Page(items=items, offset=offset, next_offset=None)
            raw_records = cast(
                list[str | None],
                await self._redis.hmget(keys.records, members),
            )

            consumed = 0
            for raw in raw_records:
                consumed += 1
                if raw is None:
                    continue
                record = decode_record(raw)
                if state is not None and record.state is not state:
                    continue
                if source is not None and source not in record.source_names:
                    continue
                items.append(record)
                if len(items) == limit:
                    break

            current += consumed
            if len(items) == limit:
                has_more = consumed < len(members) or len(members) == fetch_count
                next_offset = current if has_more and current <= 10_000 else None
                return Page(items=items, offset=offset, next_offset=next_offset)
            if len(members) < fetch_count:
                return Page(items=items, offset=offset, next_offset=None)

        return Page(items=items, offset=offset, next_offset=None)

    async def random_proxies(
        self,
        domain: str,
        min_score: int,
        count: int,
        max_latency_ms: float | None = None,
        max_checked_age_seconds: int | None = None,
        min_consecutive_successes: int = 1,
        now: datetime | None = None,
    ) -> list[ProxyRecord]:
        if count <= 0:
            raise ValueError("count must be positive")
        if not 0 <= min_score <= 100:
            raise ValueError("min_score must be within 0..100")
        if max_latency_ms is not None and max_latency_ms < 0:
            raise ValueError("max_latency_ms must be non-negative")
        if max_checked_age_seconds is not None and max_checked_age_seconds <= 0:
            raise ValueError("max_checked_age_seconds must be positive")
        if min_consecutive_successes <= 0:
            raise ValueError("min_consecutive_successes must be positive")

        requested = min(count, 20)
        checked_after = None
        if max_checked_age_seconds is not None:
            current = now or datetime.now(UTC)
            checked_after = current.timestamp() - max_checked_age_seconds
        keys = keys_for(self._prefix, domain)
        total = int(await self._redis.zcount(keys.quality, min_score, "+inf"))
        inspection_count = min(total, requested * 25)
        if inspection_count == 0:
            return []

        offsets = random.sample(range(total), inspection_count)
        pipeline = self._redis.pipeline(transaction=False)
        for offset in offsets:
            pipeline.zrevrangebyscore(keys.quality, "+inf", min_score, start=offset, num=1)
        responses = cast(list[list[str]], await pipeline.execute())
        members = list(dict.fromkeys(item[0] for item in responses if item))
        raw_records = cast(list[str | None], await self._redis.hmget(keys.records, members))

        selected: list[ProxyRecord] = []
        for raw in raw_records:
            if raw is None:
                continue
            record = decode_record(raw)
            if record.state is not ProxyState.AVAILABLE:
                continue
            if record.consecutive_successes < min_consecutive_successes:
                continue
            if max_latency_ms is not None and (
                record.latency_ewma_ms is None or record.latency_ewma_ms > max_latency_ms
            ):
                continue
            if checked_after is not None and (
                record.last_checked_at is None or record.last_checked_at.timestamp() < checked_after
            ):
                continue
            selected.append(record)
            if len(selected) == requested:
                break
        return selected

    async def stats(self, domain: str | None = None) -> PoolStats:
        if domain is None:
            totals = PoolStats()
            for item_domain in await self.list_domains():
                current = await self.stats(item_domain)
                totals = PoolStats(
                    total=totals.total + current.total,
                    candidate=totals.candidate + current.candidate,
                    available=totals.available + current.available,
                    degraded=totals.degraded + current.degraded,
                    quarantined=totals.quarantined + current.quarantined,
                    due=totals.due + current.due,
                    leased=totals.leased + current.leased,
                )
            return totals

        keys = keys_for(self._prefix, domain)
        counts = {state: 0 for state in ProxyState}
        total = 0
        cursor = 0
        while True:
            cursor, records = await self._redis.hscan(keys.records, cursor=cursor, count=200)
            for raw in cast(dict[str, str], records).values():
                record = decode_record(raw)
                counts[record.state] += 1
                total += 1
            if cursor == 0:
                break

        return PoolStats(
            total=total,
            candidate=counts[ProxyState.CANDIDATE],
            available=counts[ProxyState.AVAILABLE],
            degraded=counts[ProxyState.DEGRADED],
            quarantined=counts[ProxyState.QUARANTINED],
            due=int(await self._redis.zcard(keys.due)),
            leased=int(await self._redis.zcard(keys.leased)),
        )

    async def enforce_capacity(self, domain: str, maximum: int) -> int:
        if maximum < 1:
            raise ValueError("maximum must be positive")

        keys = keys_for(self._prefix, domain)
        total = int(await self._redis.hlen(keys.records))
        excess = total - maximum
        if excess <= 0:
            return 0

        candidates: list[tuple[tuple[int, int, float, str], str]] = []
        cursor = 0
        while True:
            cursor, raw_records = await self._redis.hscan(keys.records, cursor=cursor, count=200)
            records = cast(dict[str, str], raw_records)
            if records:
                endpoints = list(records)
                pipeline = self._redis.pipeline(transaction=False)
                for endpoint in endpoints:
                    pipeline.zscore(keys.leased, endpoint)
                lease_scores = cast(list[float | None], await pipeline.execute())

                batch: list[tuple[tuple[int, int, float, str], str]] = []
                for endpoint, lease_score in zip(endpoints, lease_scores, strict=True):
                    if lease_score is not None:
                        continue
                    record = decode_record(records[endpoint])
                    state_rank = 0 if record.state is ProxyState.QUARANTINED else 1
                    age = (
                        record.last_checked_at or record.first_seen_at
                        if record.state is ProxyState.QUARANTINED
                        else record.last_seen_at
                    )
                    ranking = (state_rank, record.score, age.timestamp(), endpoint)
                    batch.append((ranking, endpoint))
                candidates = sorted(chain(candidates, batch))[:excess]
            if cursor == 0:
                break

        endpoints_to_remove = [endpoint for _, endpoint in candidates]
        for start in range(0, len(endpoints_to_remove), 500):
            chunk = endpoints_to_remove[start : start + 500]
            pipeline = self._redis.pipeline(transaction=True)
            pipeline.hdel(keys.records, *chunk)
            pipeline.zrem(keys.quality, *chunk)
            pipeline.zrem(keys.due, *chunk)
            pipeline.zrem(keys.available_latency, *chunk)
            await pipeline.execute()
        return len(endpoints_to_remove)
