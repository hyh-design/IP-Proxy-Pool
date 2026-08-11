"""Periodic, idempotent dashboard snapshot recording."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from ip_proxy_pool.config import DashboardSettings
from ip_proxy_pool.dashboard.analytics import aggregate_records
from ip_proxy_pool.dashboard.history import DashboardHistoryStore
from ip_proxy_pool.dashboard.models import HistoryPoint, HistoryResolution
from ip_proxy_pool.models import ProxyRecord
from ip_proxy_pool.storage.repository import RedisRepository

GLOBAL_SCOPE = "__all__"

_RELEASE_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class DashboardSnapshotRecorder:
    """Write at most one dashboard snapshot for each configured time bucket."""

    def __init__(
        self,
        redis: Any,
        *,
        repository: RedisRepository,
        history: DashboardHistoryStore,
        prefix: str,
        settings: DashboardSettings,
    ) -> None:
        if not prefix:
            raise ValueError("prefix must be non-empty")
        self._redis = redis
        self._repository = repository
        self._history = history
        self._prefix = prefix
        self._settings = settings

    def _bucket_time(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("snapshot time must be timezone-aware")
        timestamp = int(value.astimezone(UTC).timestamp())
        interval = self._settings.snapshot_interval_seconds
        return datetime.fromtimestamp(timestamp - timestamp % interval, tz=UTC)

    def _coordination_keys(self, bucket: datetime) -> tuple[str, str]:
        name = str(int(bucket.timestamp()))
        base = f"{self._prefix}:dashboard:snapshot"
        return f"{base}-done:{name}", f"{base}-lock:{name}"

    async def _write_scope(
        self,
        scope: str,
        *,
        bucket: datetime,
        records: tuple[ProxyRecord, ...],
        scanned: int,
        partial: bool,
    ) -> None:
        stats = await self._repository.stats(None if scope == GLOBAL_SCOPE else scope)
        aggregate = aggregate_records(
            records,
            stats=stats,
            observed_at=bucket,
            scanned=scanned,
            partial=partial,
        )
        point = HistoryPoint.from_aggregate(aggregate)
        await self._history.write(scope, HistoryResolution.FIVE_MINUTES, point)
        if bucket.minute == 0:
            await self._history.write(scope, HistoryResolution.HOUR, point)

    async def _record(self, bucket: datetime) -> None:
        domains = await self._repository.list_domains()
        global_records: list[ProxyRecord] = []
        global_scanned = 0
        global_partial = False
        maximum = self._settings.max_aggregate_records

        for domain in domains:
            scan = await self._repository.scan_records(domain, limit=maximum)
            await self._write_scope(
                domain,
                bucket=bucket,
                records=scan.records,
                scanned=scan.scanned,
                partial=scan.partial,
            )
            remaining = maximum - len(global_records)
            if remaining > 0:
                global_records.extend(scan.records[:remaining])
                global_scanned += min(scan.scanned, remaining)
            if scan.partial or scan.scanned > remaining or remaining <= 0:
                global_partial = True

        await self._write_scope(
            GLOBAL_SCOPE,
            bucket=bucket,
            records=tuple(global_records),
            scanned=global_scanned,
            partial=global_partial,
        )

    async def record_if_due(self, now: datetime) -> bool:
        bucket = self._bucket_time(now)
        done_key, lock_key = self._coordination_keys(bucket)
        if await self._redis.exists(done_key):
            return False

        owner = uuid4().hex
        acquired = await self._redis.set(lock_key, owner, nx=True, ex=60)
        if not acquired:
            return False
        try:
            if await self._redis.exists(done_key):
                return False
            await self._record(bucket)
            await self._redis.set(done_key, "1", ex=900)
            return True
        finally:
            await self._redis.eval(_RELEASE_LOCK, 1, lock_key, owner)
