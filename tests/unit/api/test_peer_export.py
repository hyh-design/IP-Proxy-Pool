from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ip_proxy_pool.api.routes.peer import router
from ip_proxy_pool.api.routes.proxies import router as proxy_router
from ip_proxy_pool.api.routes.reclaim_quota import router as quota_router
from ip_proxy_pool.config import Settings
from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState
from ip_proxy_pool.security.rate_limit import RateLimitDecision
from ip_proxy_pool.storage.repository import LatencyIndexNotReadyError, ProxySelection


class Limiter:
    def __init__(self) -> None:
        self.namespaces: list[str] = []

    async def check(self, namespace: str, *args: Any, **kwargs: Any) -> RateLimitDecision:
        self.namespaces.append(namespace)
        return RateLimitDecision(True, 10, 9, 0)


class Repository:
    def __init__(self) -> None:
        now = datetime.now(UTC)
        self.record = ProxyRecord(
            endpoint=ProxyEndpoint.parse("1.1.1.1:80"),
            domain="portal.daqihui.com",
            score=95,
            state=ProxyState.AVAILABLE,
            source_names={"formal"},
            first_seen_at=now,
            last_seen_at=now,
            last_checked_at=now,
            next_check_at=now,
            latency_ewma_ms=100,
            consecutive_successes=3,
        )
        self.kwargs: dict[str, Any] = {}
        self.failure: Exception | None = None

    async def list_domains(self) -> list[str]:
        return ["portal.daqihui.com"]

    async def select_formal_export(self, **kwargs: Any) -> ProxySelection:
        self.kwargs = kwargs
        if self.failure:
            raise self.failure
        return ProxySelection((self.record,), 1, 1, 1, 0, 0, 0, 0)


def client(*, enabled: bool = True) -> tuple[TestClient, Repository, Limiter]:
    settings = Settings.model_validate(
        {
            "api": {"api_keys": ["read"], "admin_api_keys": ["admin"], "cursor_secret": "x" * 32},
            "reclaim_quota": {"api_keys": ["quota-one", "quota-two"]},
            "peer_export": {"enabled": enabled, "node_id": "system-one", "api_keys": ["peer"]},
            "target": {"domain": "portal.daqihui.com"},
        }
    )
    app = FastAPI()
    app.include_router(router)
    app.include_router(proxy_router)
    app.include_router(quota_router)
    repo, limiter = Repository(), Limiter()
    app.state.settings = settings
    app.state.repository = repo
    app.state.rate_limiter = limiter
    app.state.reclaim_quota_store = object()
    return TestClient(app, raise_server_exceptions=False), repo, limiter


def test_export_is_formal_scoped_with_dedicated_limit() -> None:
    api, repo, limiter = client()
    response = api.get(
        "/v1/peer/proxies", params={"domain": "portal.daqihui.com"}, headers={"X-API-Key": "peer"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["origin_node"] == "system-one"
    assert body["scope"] == "formal"
    assert len(body["items"]) == 1
    assert set(body["items"][0]) == {
        "endpoint",
        "domain",
        "score",
        "latency_ewma_ms",
        "last_checked_at",
        "consecutive_successes",
        "source_names",
    }
    assert repo.kwargs["domain"] == "portal.daqihui.com"
    assert repo.kwargs["max_latency_ms"] <= 2000
    assert limiter.namespaces == ["peer-export"]


@pytest.mark.parametrize(
    "key,status", [(None, 401), ("read", 403), ("admin", 403), ("quota-one", 403)]
)
def test_export_rejects_other_roles(key: str | None, status: int) -> None:
    api, _, _ = client()
    headers = {"X-API-Key": key} if key is not None else {}
    assert (
        api.get(
            "/v1/peer/proxies", params={"domain": "portal.daqihui.com"}, headers=headers
        ).status_code
        == status
    )


def test_peer_key_cannot_use_query_route() -> None:
    api, _, _ = client()
    assert api.get("/v1/domains", headers={"X-API-Key": "peer"}).status_code == 403
    assert (
        api.get(
            "/v1/proxies",
            params={"domain": "portal.daqihui.com"},
            headers={"X-API-Key": "peer"},
        ).status_code
        == 403
    )
    assert (
        api.post(
            "/v1/proxies/feedback",
            headers={"X-API-Key": "peer"},
            json={"domain": "portal.daqihui.com", "endpoint": "1.1.1.1:80", "outcome": "success"},
        ).status_code
        == 403
    )
    assert (
        api.post(
            "/v1/reclaim/quota/acquire",
            headers={"X-API-Key": "peer"},
            json={
                "schema_version": 1,
                "domain": "portal.daqihui.com",
                "attempt_id": "d8a73e7e-b468-4b70-ae39-e87cc0f78ab8",
            },
        ).status_code
        == 403
    )


def test_export_disabled_bad_domain_and_index_not_ready() -> None:
    disabled, _, _ = client(enabled=False)
    assert (
        disabled.get(
            "/v1/peer/proxies",
            params={"domain": "portal.daqihui.com"},
            headers={"X-API-Key": "peer"},
        ).status_code
        == 503
    )
    api, repo, _ = client()
    assert (
        api.get(
            "/v1/peer/proxies", params={"domain": "example.org"}, headers={"X-API-Key": "peer"}
        ).status_code
        == 422
    )
    repo.failure = LatencyIndexNotReadyError("not ready")
    assert (
        api.get(
            "/v1/peer/proxies",
            params={"domain": "portal.daqihui.com"},
            headers={"X-API-Key": "peer"},
        ).status_code
        == 503
    )
