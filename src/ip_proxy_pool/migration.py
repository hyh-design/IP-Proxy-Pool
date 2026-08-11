import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel
from redis.asyncio import Redis

from ip_proxy_pool.config import Settings
from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState
from ip_proxy_pool.security.network import (
    NetworkBoundaryError,
    validate_proxy_endpoint,
)
from ip_proxy_pool.storage.repository import RedisRepository


class ImportSummary(BaseModel):
    scanned: int = 0
    accepted: int = 0
    skipped_invalid: int = 0
    skipped_non_global: int = 0
    written: int = 0


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
    repository = RedisRepository(redis, prefix=settings.redis.key_prefix)
    try:
        summary = await LegacyMigrator(redis, repository).import_key(
            redis_key, domain, dry_run=dry_run
        )
        print(json.dumps(summary.model_dump(), sort_keys=True))
        return 0
    finally:
        await redis.aclose()
