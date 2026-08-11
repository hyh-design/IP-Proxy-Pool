from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ip_proxy_pool.api.dashboard_models import (
    DashboardSummary,
    QualitySummary,
    SourceSummary,
)
from ip_proxy_pool.api.routes.dashboard import router
from ip_proxy_pool.config import Settings
from ip_proxy_pool.dashboard.heartbeat import RoleHealth, RoleStatus
from ip_proxy_pool.dashboard.models import (
    HistoryPoint,
    HistoryRange,
    HistoryResolution,
    HistorySeries,
    LatencySummary,
    ScoreBuckets,
    SourceQuality,
)
from ip_proxy_pool.dashboard.service import DashboardDomainNotFound
from ip_proxy_pool.security.rate_limit import RateLimitDecision

BASE = datetime(2026, 8, 11, 8, tzinfo=UTC)
READ_HEADERS = {"X-API-Key": "read-key"}


class FakeLimiter:
    async def check(self, *args: Any, **kwargs: Any) -> RateLimitDecision:
        del args, kwargs
        return RateLimitDecision(
            allowed=True,
            limit=100,
            remaining=99,
            retry_after_seconds=0,
        )


class FakeService:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.history_range: HistoryRange | None = None

    def _raise(self, domain: str | None) -> None:
        if domain == "missing.example":
            raise DashboardDomainNotFound(domain)
        if self.error is not None:
            raise self.error

    async def summary(self, domain: str | None, *, now: datetime) -> DashboardSummary:
        del now
        self._raise(domain)
        return DashboardSummary(
            domain=domain,
            observed_at=BASE,
            total=10,
            candidate=1,
            available=7,
            degraded=1,
            quarantined=1,
            due=2,
            leased=0,
            availability_rate=7 / 9,
            available_pool_share=0.7,
            high_quality=4,
            latest_snapshot_at=BASE,
            freshness_seconds=0,
            api_status="healthy",
            redis_status="healthy",
            collector=RoleHealth(
                role="collector",
                status=RoleStatus.HEALTHY,
                active_instances=1,
                newest_heartbeat_at=BASE,
            ),
            checker=RoleHealth(
                role="checker",
                status=RoleStatus.HEALTHY,
                active_instances=1,
                newest_heartbeat_at=BASE,
            ),
            scanned=10,
            partial=False,
        )

    async def history(
        self,
        domain: str | None,
        range_: HistoryRange,
        *,
        now: datetime,
    ) -> HistorySeries:
        del now
        self._raise(domain)
        self.history_range = range_
        return HistorySeries(
            range=range_,
            resolution=HistoryResolution.FIVE_MINUTES,
            points=(
                HistoryPoint(
                    observed_at=BASE,
                    total=10,
                    candidate=1,
                    available=7,
                    degraded=1,
                    quarantined=1,
                    due=2,
                    leased=0,
                    availability_rate=7 / 9,
                    available_pool_share=0.7,
                    high_quality=4,
                    score_buckets=ScoreBuckets(low=1, watch=2, usable=3, high=4),
                    latency=LatencySummary(samples=7, average_ms=100, p50_ms=90, p95_ms=200),
                ),
            ),
        )

    async def quality(self, domain: str, *, now: datetime) -> QualitySummary:
        del now
        self._raise(domain)
        return QualitySummary(
            domain=domain,
            observed_at=BASE,
            candidate=1,
            available=7,
            degraded=1,
            quarantined=1,
            score_buckets=ScoreBuckets(low=1, watch=2, usable=3, high=4),
            latency=LatencySummary(samples=7, average_ms=100, p50_ms=90, p95_ms=200),
            scanned=20_000,
            partial=True,
        )

    async def sources(
        self,
        domain: str,
        *,
        limit: int,
        now: datetime,
    ) -> SourceSummary:
        del now
        self._raise(domain)
        return SourceSummary(
            domain=domain,
            observed_at=BASE,
            items=(
                SourceQuality(
                    name="alpha",
                    total=10,
                    tested=9,
                    available=7,
                    availability_rate=7 / 9,
                    average_score=91,
                ),
            )[:limit],
            scanned=10,
            partial=False,
        )


def make_client() -> tuple[TestClient, FakeService]:
    app = FastAPI()
    app.include_router(router)
    service = FakeService()
    app.state.settings = Settings.model_validate(
        {
            "api": {
                "api_keys": ["read-key"],
                "cursor_secret": "x" * 32,
            }
        }
    )
    app.state.rate_limiter = FakeLimiter()
    app.state.dashboard_service = service
    return TestClient(app, raise_server_exceptions=False), service


def test_dashboard_summary_requires_key() -> None:
    client, _ = make_client()

    response = client.get("/v1/dashboard/summary")

    assert response.status_code == 401


def test_history_defaults_to_24_hours_and_reports_resolution() -> None:
    client, service = make_client()

    response = client.get(
        "/v1/dashboard/history?domain=example.com",
        headers=READ_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["range"] == "24h"
    assert response.json()["resolution"] == "5m"
    assert service.history_range is HistoryRange.H24


def test_history_rejects_unlisted_range() -> None:
    client, _ = make_client()

    response = client.get(
        "/v1/dashboard/history?range=48h",
        headers=READ_HEADERS,
    )

    assert response.status_code == 422


def test_quality_marks_a_bounded_scan_as_partial() -> None:
    client, _ = make_client()

    response = client.get(
        "/v1/dashboard/quality?domain=example.com",
        headers=READ_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["partial"] is True
    assert response.json()["scanned"] == 20_000


def test_unknown_dashboard_domain_is_404() -> None:
    client, _ = make_client()

    response = client.get(
        "/v1/dashboard/summary?domain=missing.example",
        headers=READ_HEADERS,
    )

    assert response.status_code == 404


def test_dashboard_storage_failure_is_sanitized() -> None:
    client, service = make_client()
    service.error = RuntimeError("redis://user:secret@host")

    response = client.get("/v1/dashboard/summary", headers=READ_HEADERS)

    assert response.status_code == 503
    assert response.json() == {"detail": "service unavailable"}


def test_summary_role_health_does_not_expose_instance_ids() -> None:
    client, _ = make_client()

    body = client.get("/v1/dashboard/summary", headers=READ_HEADERS).json()

    assert body["collector"]["active_instances"] == 1
    assert "instance_ids" not in body["collector"]
    assert "instance_ids" not in body["checker"]


def test_sources_limit_above_hard_max_is_rejected() -> None:
    client, _ = make_client()

    response = client.get(
        "/v1/dashboard/sources?domain=example.com&limit=101",
        headers=READ_HEADERS,
    )

    assert response.status_code == 422


def test_history_etag_returns_not_modified() -> None:
    client, _ = make_client()
    first = client.get("/v1/dashboard/history", headers=READ_HEADERS)

    second = client.get(
        "/v1/dashboard/history",
        headers={**READ_HEADERS, "If-None-Match": first.headers["etag"]},
    )

    assert first.status_code == 200
    assert second.status_code == 304
    assert second.content == b""
