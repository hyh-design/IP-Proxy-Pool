from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from ip_proxy_pool.api.app import create_app
from ip_proxy_pool.config import Settings
from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState
from ip_proxy_pool.security.rate_limit import RateLimitDecision
from ip_proxy_pool.storage.repository import Page


class Limiter:
    async def check(self, *args: Any, **kwargs: Any) -> RateLimitDecision:
        del args, kwargs
        return RateLimitDecision(True, 100, 99, 0)


class Repository:
    def __init__(self) -> None:
        now = datetime(2026, 8, 10, tzinfo=UTC)
        self.record = ProxyRecord(
            endpoint=ProxyEndpoint.parse("1.1.1.1:80"),
            domain="example.com",
            score=90,
            state=ProxyState.AVAILABLE,
            first_seen_at=now,
            last_seen_at=now,
            next_check_at=now,
        )

    async def list_domains(self) -> list[str]:
        return ["example.com"]

    async def list_proxies(self, **kwargs: Any) -> Page[ProxyRecord]:
        return Page(items=[self.record], offset=int(kwargs["offset"]), next_offset=None)


def settings(enabled: bool = False) -> Settings:
    return Settings.model_validate(
        {
            "api": {
                "api_keys": ["read-key"],
                "cursor_secret": "x" * 32,
                "legacy_routes_enabled": enabled,
            }
        }
    )


def test_legacy_routes_are_off_by_default() -> None:
    paths = set(create_app(settings()).openapi()["paths"])

    assert "/proxy/{num}" not in paths
    assert "/all" not in paths


def test_enabled_legacy_route_is_authenticated_and_deprecated() -> None:
    app = create_app(settings(enabled=True))
    app.state.settings = settings(enabled=True)
    app.state.repository = Repository()
    app.state.rate_limiter = Limiter()
    client = TestClient(app, raise_server_exceptions=False)

    unauthorized = client.get("/all", params={"domain": "example.com"})
    response = client.get(
        "/all",
        params={"domain": "example.com"},
        headers={"X-API-Key": "read-key"},
    )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json() == ["1.1.1.1:80"]
    assert response.headers["deprecation"] == "true"
    assert "sunset" in response.headers


def test_legacy_num_is_capped_and_test_route_absent() -> None:
    app = create_app(settings(enabled=True))
    app.state.settings = settings(enabled=True)
    app.state.repository = Repository()
    app.state.rate_limiter = Limiter()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(
        "/proxy/201",
        params={"domain": "example.com"},
        headers={"X-API-Key": "read-key"},
    )

    assert response.status_code == 422
    assert "/test" not in app.openapi()["paths"]
