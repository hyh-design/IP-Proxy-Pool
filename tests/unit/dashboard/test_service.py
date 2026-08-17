from datetime import UTC, datetime

import pytest

from ip_proxy_pool.config import DashboardSettings, SelectionSettings
from ip_proxy_pool.dashboard.heartbeat import RoleHealth, RoleStatus
from ip_proxy_pool.dashboard.models import HistoryRange, HistoryResolution, HistorySeries
from ip_proxy_pool.dashboard.service import DashboardDomainNotFound, DashboardService
from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState
from ip_proxy_pool.storage.repository import PoolStats, RecordScan, SelectionCounts

BASE = datetime(2026, 8, 11, 8, tzinfo=UTC)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.writes: list[tuple[str, int]] = []

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.values[key] = value
        self.writes.append((key, ttl))

    async def ping(self) -> bool:
        return True


class FakeRepository:
    def __init__(self) -> None:
        self.scan_calls = 0
        self.record = ProxyRecord(
            endpoint=ProxyEndpoint.parse("1.1.1.1:80"),
            domain="example.com",
            score=95,
            state=ProxyState.AVAILABLE,
            source_names={"alpha"},
            first_seen_at=BASE,
            last_seen_at=BASE,
            last_checked_at=BASE,
            next_check_at=BASE,
            consecutive_successes=2,
            latency_ewma_ms=100,
        )

    async def list_domains(self) -> list[str]:
        return ["example.com"]

    async def stats(self, domain: str | None = None) -> PoolStats:
        del domain
        return PoolStats(total=1, available=1, due=1)

    async def scan_records(self, domain: str, *, limit: int) -> RecordScan:
        del domain, limit
        self.scan_calls += 1
        return RecordScan(records=(self.record,), scanned=1, partial=False)

    async def selection_counts(self, *args: object, **kwargs: object) -> SelectionCounts:
        del args, kwargs
        return SelectionCounts(indexed=3, candidates=2, selectable=1)


class FakeHistory:
    async def read(
        self,
        scope: str,
        range_: HistoryRange,
        *,
        now: datetime,
    ) -> HistorySeries:
        del scope, now
        resolution = (
            HistoryResolution.HOUR
            if range_ in {HistoryRange.D7, HistoryRange.D30}
            else HistoryResolution.FIVE_MINUTES
        )
        return HistorySeries(range=range_, resolution=resolution, points=())


class FakeHeartbeat:
    async def health(self, role: str, *, now: datetime) -> RoleHealth:
        return RoleHealth(
            role=role,
            status=RoleStatus.HEALTHY,
            active_instances=1,
            newest_heartbeat_at=now,
        )


def build_service(
    settings: DashboardSettings | None = None,
) -> tuple[DashboardService, FakeRedis, FakeRepository]:
    redis = FakeRedis()
    repository = FakeRepository()
    return (
        DashboardService(
            redis,
            repository=repository,
            history=FakeHistory(),
            heartbeat=FakeHeartbeat(),
            prefix="ippool:test",
            settings=settings or DashboardSettings(),
            selection=SelectionSettings(max_latency_ms=1000),
        ),
        redis,
        repository,
    )


async def test_summary_cache_uses_ten_second_ttl_and_hashed_scope() -> None:
    service, redis, repository = build_service()

    first = await service.summary("example.com", now=BASE)
    second = await service.summary("example.com", now=BASE)

    assert first == second
    assert repository.scan_calls == 1
    assert redis.writes[0][1] == 10
    assert "example.com" not in redis.writes[0][0]


async def test_quality_and_sources_cache_for_thirty_seconds() -> None:
    service, redis, _ = build_service()

    await service.quality("example.com", now=BASE)
    await service.quality("example.com", now=BASE)
    await service.sources("example.com", limit=20, now=BASE)
    await service.sources("example.com", limit=20, now=BASE)

    assert [ttl for _, ttl in redis.writes] == [30, 30]
    assert all("example.com" not in key for key, _ in redis.writes)


async def test_service_rejects_unknown_domain() -> None:
    service, _, _ = build_service()

    with pytest.raises(DashboardDomainNotFound):
        await service.quality("missing.example", now=BASE)


async def test_summary_exposes_configured_browser_refresh_interval() -> None:
    service, _, _ = build_service(DashboardSettings(refresh_seconds=45))

    result = await service.summary("example.com", now=BASE)

    assert result.refresh_seconds == 45


async def test_summary_distinguishes_available_indexed_and_selectable_counts() -> None:
    service, _, _ = build_service()

    result = await service.summary("example.com", now=BASE)

    assert result.available == 1
    assert result.latency_indexed == 3
    assert result.selectable == 1
