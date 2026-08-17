from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ip_proxy_pool.api.cursors import CursorCodec
from ip_proxy_pool.api.routes.proxies import router as proxy_router
from ip_proxy_pool.api.routes.stats import router as stats_router
from ip_proxy_pool.config import Settings
from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState
from ip_proxy_pool.observability.metrics import NoopMetrics
from ip_proxy_pool.security.rate_limit import RateLimitDecision
from ip_proxy_pool.storage.repository import (
    LatencyIndexNotReadyError,
    Page,
    PoolStats,
    ProxySelection,
)


class FakeLimiter:
    def __init__(self, *, allowed: bool = True, error: Exception | None = None) -> None:
        self.allowed = allowed
        self.error = error

    async def check(self, *args: Any, **kwargs: Any) -> RateLimitDecision:
        del args, kwargs
        if self.error is not None:
            raise self.error
        return RateLimitDecision(
            allowed=self.allowed,
            limit=10,
            remaining=0 if not self.allowed else 9,
            retry_after_seconds=10,
        )


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        del ttl
        self.values[key] = value


class FakeRepository:
    def __init__(self) -> None:
        self.fail: Exception | None = None
        self.random_fail: Exception | None = None
        self.random_kwargs: dict[str, Any] = {}
        self.saved: ProxyRecord | None = None
        now = datetime(2026, 8, 10, tzinfo=UTC)
        self.records = [
            ProxyRecord(
                endpoint=ProxyEndpoint.parse("1.1.1.1:80"),
                domain="example.com",
                score=90,
                state=ProxyState.AVAILABLE,
                source_names={"alpha"},
                consecutive_successes=3,
                first_seen_at=now,
                last_seen_at=now,
                last_checked_at=now,
                latency_ewma_ms=1000,
                next_check_at=now,
            )
        ]

    async def list_domains(self) -> list[str]:
        if self.fail:
            raise self.fail
        return ["example.com"]

    async def list_proxies(self, **kwargs: Any) -> Page[ProxyRecord]:
        if self.fail:
            raise self.fail
        offset = int(kwargs["offset"])
        return Page(
            items=self.records,
            offset=offset,
            next_offset=offset + 1 if offset == 0 else None,
        )

    async def select_random_proxies(self, *args: Any, **kwargs: Any) -> ProxySelection:
        del args
        self.random_kwargs = kwargs
        if self.random_fail:
            raise self.random_fail
        if self.fail:
            raise self.fail
        return ProxySelection(tuple(self.records), 1, 1, 1, 0, 0, 0, 0)

    async def get_record(self, domain: str, endpoint: str) -> ProxyRecord | None:
        return next(
            (
                record
                for record in self.records
                if record.domain == domain and record.endpoint.canonical == endpoint
            ),
            None,
        )

    async def save_record(self, record: ProxyRecord) -> None:
        self.saved = record
        self.records = [
            record if item.endpoint == record.endpoint else item for item in self.records
        ]

    async def stats(self, domain: str | None = None) -> PoolStats:
        del domain
        if self.fail:
            raise self.fail
        return PoolStats(total=1, available=1, due=1)


def settings() -> Settings:
    return Settings.model_validate(
        {
            "api": {
                "api_keys": ["read-key"],
                "admin_api_keys": ["admin-key"],
                "cursor_secret": "x" * 32,
            }
        }
    )


def make_client(
    *,
    limiter: FakeLimiter | None = None,
    repository: FakeRepository | None = None,
) -> tuple[TestClient, FakeRepository]:
    app = FastAPI()
    app.include_router(proxy_router)
    app.include_router(stats_router)
    repo = repository or FakeRepository()
    app.state.settings = settings()
    app.state.repository = repo
    app.state.rate_limiter = limiter or FakeLimiter()
    app.state.cursor_codec = CursorCodec(secret=b"x" * 32)
    app.state.redis = FakeRedis()
    app.state.metrics = NoopMetrics()
    return TestClient(app, raise_server_exceptions=False), repo


def test_business_route_requires_key() -> None:
    client, _ = make_client()

    response = client.get("/v1/domains")

    assert response.status_code == 401


def test_proxy_limit_above_max_is_rejected() -> None:
    client, _ = make_client()

    response = client.get(
        "/v1/proxies",
        params={"domain": "example.com", "limit": 201},
        headers={"X-API-Key": "read-key"},
    )

    assert response.status_code == 422


def test_proxy_details_filter_by_score_range_and_source() -> None:
    client, _ = make_client()

    response = client.get(
        "/v1/proxies",
        params={
            "domain": "example.com",
            "state": "available",
            "min_score": 80,
            "max_score": 95,
            "source": "alpha",
        },
        headers={"X-API-Key": "read-key"},
    )

    assert response.status_code == 200
    assert [item["endpoint"] for item in response.json()["items"]] == ["1.1.1.1:80"]


def test_cursor_cannot_be_reused_with_a_different_source_filter() -> None:
    client, _ = make_client()
    first = client.get(
        "/v1/proxies?domain=example.com&source=alpha&limit=1",
        headers={"X-API-Key": "read-key"},
    ).json()

    response = client.get(
        "/v1/proxies",
        params={
            "domain": "example.com",
            "source": "beta",
            "limit": 1,
            "cursor": first["next_cursor"],
        },
        headers={"X-API-Key": "read-key"},
    )

    assert response.status_code == 422


def test_proxy_score_range_must_be_ordered() -> None:
    client, _ = make_client()

    response = client.get(
        "/v1/proxies?domain=example.com&min_score=96&max_score=95",
        headers={"X-API-Key": "read-key"},
    )

    assert response.status_code == 422


def test_proxy_source_filter_has_a_hard_length_limit() -> None:
    client, _ = make_client()

    response = client.get(
        "/v1/proxies",
        params={"domain": "example.com", "source": "x" * 65},
        headers={"X-API-Key": "read-key"},
    )

    assert response.status_code == 422


def test_unknown_domain_is_404() -> None:
    client, _ = make_client()

    response = client.get(
        "/v1/proxies",
        params={"domain": "unknown.example"},
        headers={"X-API-Key": "read-key"},
    )

    assert response.status_code == 404


def test_proxy_cursor_advances_and_is_accepted() -> None:
    client, _ = make_client()
    first = client.get(
        "/v1/proxies",
        params={"domain": "example.com", "limit": 1},
        headers={"X-API-Key": "read-key"},
    )

    cursor = first.json()["next_cursor"]
    second = client.get(
        "/v1/proxies",
        params={"domain": "example.com", "limit": 1, "cursor": cursor},
        headers={"X-API-Key": "read-key"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["next_cursor"] is None


def test_random_count_is_hard_bounded() -> None:
    client, _ = make_client()

    response = client.get(
        "/v1/proxies/random",
        params={"domain": "example.com", "count": 21},
        headers={"X-API-Key": "read-key"},
    )

    assert response.status_code == 422


def test_random_route_enforces_configured_hot_pool_thresholds() -> None:
    client, repository = make_client()

    response = client.get(
        "/v1/proxies/random",
        params={"domain": "example.com", "count": 5, "min_score": 80},
        headers={"X-API-Key": "read-key"},
    )

    assert response.status_code == 200
    assert repository.random_kwargs == {
        "domain": "example.com",
        "min_score": 90,
        "count": 5,
        "max_latency_ms": 5000,
        "max_checked_age_seconds": 600,
        "min_consecutive_successes": 2,
    }


def test_random_route_allows_only_stricter_hot_pool_thresholds() -> None:
    client, repository = make_client()

    response = client.get(
        "/v1/proxies/random",
        params={
            "domain": "example.com",
            "count": 5,
            "min_score": 95,
            "max_latency_ms": 3000,
            "max_checked_age_seconds": 300,
            "min_consecutive_successes": 3,
        },
        headers={"X-API-Key": "read-key"},
    )

    assert response.status_code == 200
    assert repository.random_kwargs == {
        "domain": "example.com",
        "min_score": 95,
        "count": 5,
        "max_latency_ms": 3000,
        "max_checked_age_seconds": 300,
        "min_consecutive_successes": 3,
    }


def test_random_route_reports_unbuilt_latency_index_without_fallback() -> None:
    repository = FakeRepository()
    repository.random_fail = LatencyIndexNotReadyError("latency index not ready")
    client, _ = make_client(repository=repository)

    response = client.get(
        "/v1/proxies/random",
        params={"domain": "example.com"},
        headers={"X-API-Key": "read-key"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "latency index not ready"}


def test_proxy_failure_feedback_immediately_removes_hot_eligibility() -> None:
    client, repository = make_client()

    response = client.post(
        "/v1/proxies/feedback",
        json={
            "domain": "example.com",
            "endpoint": "1.1.1.1:80",
            "outcome": "proxy_error",
            "error_type": "connection_reset",
        },
        headers={"X-API-Key": "read-key"},
    )

    assert response.status_code == 200
    assert repository.saved is not None
    assert repository.saved.consecutive_successes == 0
    assert repository.saved.failure_count == 1
    assert repository.saved.state is ProxyState.QUARANTINED
    delay = (repository.saved.next_check_at - repository.saved.last_checked_at).total_seconds()
    assert 21600 <= delay <= 86400


def test_two_success_feedback_events_confirm_candidate() -> None:
    client, repository = make_client()
    repository.records[0] = repository.records[0].model_copy(
        update={
            "score": 50,
            "state": ProxyState.CANDIDATE,
            "consecutive_successes": 0,
        }
    )
    body = {
        "domain": "example.com",
        "endpoint": "1.1.1.1:80",
        "outcome": "success",
        "latency_ms": 100,
        "status_code": 200,
    }

    first = client.post(
        "/v1/proxies/feedback",
        json=body,
        headers={"X-API-Key": "read-key"},
    )
    second = client.post(
        "/v1/proxies/feedback",
        json=body,
        headers={"X-API-Key": "read-key"},
    )

    assert first.json()["state"] == "candidate"
    assert second.json()["state"] == "available"
    assert repository.saved is not None
    assert repository.saved.consecutive_successes == 2


def test_rate_limit_rejection_has_retry_after() -> None:
    client, _ = make_client(limiter=FakeLimiter(allowed=False))

    response = client.get("/v1/domains", headers={"X-API-Key": "read-key"})

    assert response.status_code == 429
    assert response.headers["retry-after"] == "10"


def test_internal_repository_error_is_sanitized() -> None:
    repository = FakeRepository()
    repository.fail = RuntimeError("redis password=secret")
    client, _ = make_client(repository=repository)

    response = client.get("/v1/domains", headers={"X-API-Key": "read-key"})

    assert response.status_code == 503
    assert "secret" not in response.text


def test_stats_are_returned_and_cached() -> None:
    client, _ = make_client()

    first = client.get("/v1/stats", headers={"X-API-Key": "read-key"})
    second = client.get("/v1/stats", headers={"X-API-Key": "read-key"})

    assert first.status_code == 200
    assert second.json()["available"] == 1
