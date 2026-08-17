"""Dashboard service facade for bounded storage access and response caching."""

import hashlib
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, TypeVar

from pydantic import BaseModel

from ip_proxy_pool.api.dashboard_models import (
    DashboardSummary,
    QualitySummary,
    SourceSummary,
)
from ip_proxy_pool.config import DashboardSettings, SelectionSettings
from ip_proxy_pool.dashboard.analytics import aggregate_records
from ip_proxy_pool.dashboard.heartbeat import WorkerHeartbeatStore
from ip_proxy_pool.dashboard.history import DashboardHistoryStore
from ip_proxy_pool.dashboard.models import DashboardAggregate, HistoryRange, HistorySeries
from ip_proxy_pool.dashboard.snapshot import GLOBAL_SCOPE
from ip_proxy_pool.models import ProxyRecord
from ip_proxy_pool.storage.repository import LatencyIndexNotReadyError, RedisRepository

ModelT = TypeVar("ModelT", bound=BaseModel)


class DashboardDomainNotFound(LookupError):
    """Raised when a dashboard query names an unknown proxy domain."""


class DashboardService:
    def __init__(
        self,
        redis: Any,
        *,
        repository: RedisRepository,
        history: DashboardHistoryStore,
        heartbeat: WorkerHeartbeatStore,
        prefix: str,
        settings: DashboardSettings,
        selection: SelectionSettings,
    ) -> None:
        if not prefix:
            raise ValueError("prefix must be non-empty")
        self._redis = redis
        self._repository = repository
        self._history = history
        self._heartbeat = heartbeat
        self._prefix = prefix
        self._settings = settings
        self._selection = selection

    async def _selection_counts(
        self, domain: str | None, *, now: datetime
    ) -> tuple[int, int, bool]:
        domains = [domain] if domain is not None else await self._repository.list_domains()
        indexed = 0
        selectable = 0
        partial = False
        for item_domain in domains:
            try:
                counts = await self._repository.selection_counts(
                    item_domain,
                    min_score=self._selection.min_score,
                    max_latency_ms=self._selection.max_latency_ms,
                    max_checked_age_seconds=self._selection.max_checked_age_seconds,
                    min_consecutive_successes=self._selection.min_consecutive_successes,
                    now=now,
                )
            except LatencyIndexNotReadyError:
                partial = True
                continue
            indexed += counts.indexed
            selectable += counts.selectable
        return indexed, selectable, partial

    def _cache_key(self, kind: str, scope: str, *parts: object) -> str:
        raw = "|".join((kind, scope, *(str(part) for part in parts)))
        digest = hashlib.sha256(raw.encode()).hexdigest()
        return f"{self._prefix}:api:dashboard:{kind}:{digest}"

    async def _cached(
        self,
        model: type[ModelT],
        key: str,
        ttl: int,
        build: Callable[[], Awaitable[ModelT]],
    ) -> ModelT:
        cached = await self._redis.get(key)
        if cached is not None:
            try:
                return model.model_validate_json(cached)
            except ValueError:
                pass
        value = await build()
        await self._redis.setex(key, ttl, value.model_dump_json())
        return value

    async def _require_domain(self, domain: str) -> None:
        if domain not in await self._repository.list_domains():
            raise DashboardDomainNotFound(domain)

    async def _aggregate(self, domain: str | None, *, now: datetime) -> DashboardAggregate:
        if domain is not None:
            await self._require_domain(domain)
            stats = await self._repository.stats(domain)
            scan = await self._repository.scan_records(
                domain,
                limit=self._settings.max_aggregate_records,
            )
            return aggregate_records(
                scan.records,
                stats=stats,
                observed_at=now,
                scanned=scan.scanned,
                partial=scan.partial,
            )

        domains = await self._repository.list_domains()
        stats = await self._repository.stats(None)
        records: list[ProxyRecord] = []
        scanned = 0
        partial = False
        maximum = self._settings.max_aggregate_records
        for item_domain in domains:
            scan = await self._repository.scan_records(item_domain, limit=maximum)
            remaining = maximum - len(records)
            if remaining > 0:
                included = scan.records[:remaining]
                records.extend(included)
                scanned += len(included)
            if scan.partial or scan.scanned > remaining or remaining <= 0:
                partial = True
        return aggregate_records(
            records,
            stats=stats,
            observed_at=now,
            scanned=scanned,
            partial=partial,
        )

    async def summary(self, domain: str | None, *, now: datetime) -> DashboardSummary:
        scope = domain or GLOBAL_SCOPE
        key = self._cache_key("summary", scope)

        async def build() -> DashboardSummary:
            aggregate = await self._aggregate(domain, now=now)
            latency_indexed, selectable, selection_partial = await self._selection_counts(
                domain, now=now
            )
            history = await self._history.read(scope, HistoryRange.H24, now=now)
            latest = history.points[-1].observed_at if history.points else None
            freshness = None if latest is None else max(0.0, (now - latest).total_seconds())
            redis_status = "healthy" if await self._redis.ping() else "down"
            collector = await self._heartbeat.health("collector", now=now)
            checker = await self._heartbeat.health("checker", now=now)
            return DashboardSummary(
                domain=domain,
                observed_at=aggregate.observed_at,
                total=aggregate.total,
                candidate=aggregate.candidate,
                available=aggregate.available,
                latency_indexed=latency_indexed,
                selectable=selectable,
                degraded=aggregate.degraded,
                quarantined=aggregate.quarantined,
                due=aggregate.due,
                leased=aggregate.leased,
                availability_rate=aggregate.availability_rate,
                available_pool_share=aggregate.available_pool_share,
                high_quality=aggregate.high_quality,
                latest_snapshot_at=latest,
                freshness_seconds=freshness,
                api_status="healthy",
                redis_status=redis_status,
                collector=collector,
                checker=checker,
                refresh_seconds=self._settings.refresh_seconds,
                scanned=aggregate.scanned,
                partial=aggregate.partial or selection_partial,
            )

        return await self._cached(DashboardSummary, key, 10, build)

    async def history(
        self,
        domain: str | None,
        range_: HistoryRange,
        *,
        now: datetime,
    ) -> HistorySeries:
        if domain is not None:
            await self._require_domain(domain)
        return await self._history.read(domain or GLOBAL_SCOPE, range_, now=now)

    async def quality(self, domain: str, *, now: datetime) -> QualitySummary:
        key = self._cache_key("quality", domain)

        async def build() -> QualitySummary:
            aggregate = await self._aggregate(domain, now=now)
            return QualitySummary(
                domain=domain,
                observed_at=aggregate.observed_at,
                candidate=aggregate.candidate,
                available=aggregate.available,
                degraded=aggregate.degraded,
                quarantined=aggregate.quarantined,
                score_buckets=aggregate.score_buckets,
                latency=aggregate.latency,
                scanned=aggregate.scanned,
                partial=aggregate.partial,
            )

        return await self._cached(QualitySummary, key, 30, build)

    async def sources(
        self,
        domain: str,
        *,
        limit: int,
        now: datetime,
    ) -> SourceSummary:
        if not 1 <= limit <= 100:
            raise ValueError("source limit must be within 1..100")
        key = self._cache_key("sources", domain, limit)

        async def build() -> SourceSummary:
            aggregate = await self._aggregate(domain, now=now)
            return SourceSummary(
                domain=domain,
                observed_at=aggregate.observed_at,
                items=aggregate.sources[:limit],
                scanned=aggregate.scanned,
                partial=aggregate.partial,
            )

        return await self._cached(SourceSummary, key, 30, build)
