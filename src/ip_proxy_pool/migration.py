import asyncio
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from pydantic import BaseModel
from redis.asyncio import Redis

from ip_proxy_pool.config import Settings
from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState
from ip_proxy_pool.security.network import (
    NetworkBoundaryError,
    validate_proxy_endpoint,
)
from ip_proxy_pool.storage.codec import decode_record
from ip_proxy_pool.storage.keys import keys_for
from ip_proxy_pool.storage.lua import REPLACE_SELECTION_INDEXES
from ip_proxy_pool.storage.repository import (
    LATENCY_INDEX_SCHEMA_VERSION,
    PRIORITY_DUE_INDEX_SCHEMA_VERSION,
    RedisRepository,
    latency_index_score,
    priority_due_score,
)


class ImportSummary(BaseModel):
    scanned: int = 0
    accepted: int = 0
    skipped_invalid: int = 0
    skipped_non_global: int = 0
    written: int = 0


class LatencyIndexRebuildSummary(BaseModel):
    domain: str
    scanned: int = 0
    indexed: int = 0
    priority_indexed: int = 0
    ignored: int = 0
    dry_run: bool = False
    duration_seconds: float = 0.0


class LatencyIndexRebuilder:
    def __init__(
        self,
        redis: Any,
        *,
        prefix: str,
        priority_max_latency_ms: float = 1000.0,
    ) -> None:
        if not prefix:
            raise ValueError("prefix must be non-empty")
        self._redis = redis
        self._prefix = prefix
        self._priority_max_latency_ms = priority_max_latency_ms

    async def rebuild(
        self,
        domain: str,
        *,
        dry_run: bool = False,
    ) -> LatencyIndexRebuildSummary:
        if not domain:
            raise ValueError("domain must be non-empty")
        started = time.perf_counter()
        keys = keys_for(self._prefix, domain)
        temporary_key = f"{keys.available_latency}:rebuild:{uuid4().hex}"
        priority_temporary_key = f"{keys.priority_due}:rebuild:{uuid4().hex}"
        scanned = 0
        indexed = 0
        priority_indexed = 0
        ignored = 0
        cursor = 0
        try:
            while True:
                cursor, raw_records = await self._redis.hscan(
                    keys.records, cursor=cursor, count=500
                )
                eligible: dict[str, float] = {}
                priority_eligible: dict[str, float] = {}
                for endpoint, payload in cast(dict[str, str], raw_records).items():
                    scanned += 1
                    try:
                        record = decode_record(payload)
                    except (TypeError, ValueError):
                        ignored += 1
                        continue
                    latency = latency_index_score(record)
                    if latency is None:
                        ignored += 1
                        continue
                    eligible[endpoint] = latency
                    priority_due_at = priority_due_score(
                        record,
                        self._priority_max_latency_ms,
                    )
                    if priority_due_at is not None:
                        priority_eligible[endpoint] = priority_due_at
                indexed += len(eligible)
                priority_indexed += len(priority_eligible)
                if eligible and not dry_run:
                    await self._redis.zadd(temporary_key, eligible)
                if priority_eligible and not dry_run:
                    await self._redis.zadd(priority_temporary_key, priority_eligible)
                if cursor == 0:
                    break

            if not dry_run:
                written, priority_written = cast(
                    list[int],
                    await self._redis.eval(
                        REPLACE_SELECTION_INDEXES,
                        6,
                        temporary_key,
                        keys.available_latency,
                        keys.available_latency_ready,
                        priority_temporary_key,
                        keys.priority_due,
                        keys.priority_due_ready,
                        LATENCY_INDEX_SCHEMA_VERSION,
                        PRIORITY_DUE_INDEX_SCHEMA_VERSION,
                    ),
                )
                if written != indexed:
                    raise RuntimeError("latency index rebuild count mismatch")
                if priority_written != priority_indexed:
                    raise RuntimeError("priority due index rebuild count mismatch")
            return LatencyIndexRebuildSummary(
                domain=domain,
                scanned=scanned,
                indexed=indexed,
                priority_indexed=priority_indexed,
                ignored=ignored,
                dry_run=dry_run,
                duration_seconds=max(0.0, time.perf_counter() - started),
            )
        finally:
            await self._redis.delete(temporary_key, priority_temporary_key)


class LegacyMigrator:
    def __init__(
        self,
        redis: Any,
        repository: RedisRepository,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._redis = redis
        self._repository = repository
        self._now = now

    async def import_key(
        self,
        redis_key: str,
        domain: str,
        *,
        dry_run: bool = False,
    ) -> ImportSummary:
        if not redis_key or not domain:
            raise ValueError("redis_key and domain must be non-empty")
        summary = ImportSummary()
        batch: list[ProxyRecord] = []
        cursor = 0
        observed_at = self._now()
        while True:
            cursor, raw_items = await self._redis.zscan(redis_key, cursor=cursor, count=500)
            for member, raw_score in cast(list[tuple[str, float]], raw_items):
                summary.scanned += 1
                try:
                    endpoint = ProxyEndpoint.parse(member)
                except ValueError:
                    summary.skipped_invalid += 1
                    continue
                try:
                    validate_proxy_endpoint(endpoint)
                except NetworkBoundaryError:
                    summary.skipped_non_global += 1
                    continue
                summary.accepted += 1
                record = ProxyRecord(
                    endpoint=endpoint,
                    domain=domain,
                    score=max(0, min(70, int(raw_score))),
                    state=ProxyState.CANDIDATE,
                    source_names={f"legacy:{redis_key}"},
                    first_seen_at=observed_at,
                    last_seen_at=observed_at,
                    next_check_at=observed_at,
                )
                if (
                    not dry_run
                    and await self._repository.get_record(domain, endpoint.canonical) is None
                ):
                    batch.append(record)
                if len(batch) == 500:
                    summary.written += await self._write_batch(batch)
                    batch.clear()
            if cursor == 0:
                break
        if batch:
            summary.written += await self._write_batch(batch)
        return summary

    async def _write_batch(self, records: list[ProxyRecord]) -> int:
        await asyncio.gather(*(self._repository.upsert_candidate(record) for record in records))
        return len(records)


async def run_legacy_import(
    settings: Settings,
    *,
    redis_key: str,
    domain: str,
    dry_run: bool,
) -> int:
    redis = Redis.from_url(str(settings.redis.url), decode_responses=True)
    repository = RedisRepository(
        redis,
        prefix=settings.redis.key_prefix,
        priority_max_latency_ms=settings.selection.max_latency_ms,
    )
    try:
        summary = await LegacyMigrator(redis, repository).import_key(
            redis_key, domain, dry_run=dry_run
        )
        print(json.dumps(summary.model_dump(), sort_keys=True))
        return 0
    finally:
        await redis.aclose()


async def run_latency_index_rebuild(
    settings: Settings,
    *,
    domain: str,
    dry_run: bool,
) -> int:
    redis = Redis.from_url(str(settings.redis.url), decode_responses=True)
    try:
        summary = await LatencyIndexRebuilder(
            redis,
            prefix=settings.redis.key_prefix,
            priority_max_latency_ms=settings.selection.max_latency_ms,
        ).rebuild(domain, dry_run=dry_run)
        print(json.dumps(summary.model_dump(), sort_keys=True))
        return 0
    finally:
        await redis.aclose()
