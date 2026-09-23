from uuid import uuid4

from fastapi.testclient import TestClient

from ip_proxy_pool.api.app import create_app
from ip_proxy_pool.config import Settings
from ip_proxy_pool.security.rate_limit import RateLimitDecision


class PermissiveLimiter:
    async def check(self, *args: object, **kwargs: object) -> RateLimitDecision:
        return RateLimitDecision(allowed=True, limit=60, remaining=59, retry_after_seconds=0)


class DenyingLimiter:
    async def check(self, *args: object, **kwargs: object) -> RateLimitDecision:
        return RateLimitDecision(allowed=False, limit=1, remaining=0, retry_after_seconds=22)


def settings(*, enabled: bool = True) -> Settings:
    return Settings.model_validate(
        {
            "api": {
                "api_keys": ["read-key"],
                "admin_api_keys": ["admin-key"],
                "cursor_secret": "x" * 32,
            },
            "reclaim_quota": {"api_keys": ["quota-one", "quota-two"]},
            "peer_export": {"api_keys": ["export-key"]},
            "ownership": {
                "enabled": enabled,
                "members": {
                    "one:a": {"system_id": "system-one", "api_key": "own-a"},
                    "two:b": {"system_id": "system-two", "api_key": "own-b"},
                },
            },
        }
    )


def make_client(*, enabled: bool = True) -> TestClient:
    configured = settings(enabled=enabled)
    app = create_app(configured)
    app.state.settings = configured
    app.state.rate_limiter = PermissiveLimiter()
    app.state.ownership_store = object()
    return TestClient(app, raise_server_exceptions=False)


def body() -> dict[str, object]:
    return {
        "schema_version": 1,
        "case_id": str(uuid4()),
        "cycle": {
            "domain": "portal.daqihui.com",
            "lead_code": "L-42",
            "record_id": 42,
            "entered_public_pool_at": "2026-09-23 08:00:00",
        },
    }


def test_ownership_api_rejects_other_roles_and_unknown_key() -> None:
    client = make_client()
    path = "/v1/reclaim/ownership/cases"
    assert client.post(path, json=body()).status_code == 401
    for key in ("read-key", "admin-key", "quota-one", "export-key"):
        assert client.post(path, json=body(), headers={"X-API-Key": key}).status_code == 403
    assert client.post(path, json=body(), headers={"X-API-Key": "unknown"}).status_code == 401
    quota_body = {
        "schema_version": 1,
        "domain": "portal.daqihui.com",
        "attempt_id": str(uuid4()),
    }
    assert (
        client.post(
            "/v1/reclaim/quota/acquire", json=quota_body, headers={"X-API-Key": "own-a"}
        ).status_code
        == 403
    )
    assert client.get("/v1/domains", headers={"X-API-Key": "own-a"}).status_code == 403


def test_ownership_api_validates_cycle_id_and_schema() -> None:
    client = make_client()
    headers = {"X-API-Key": "own-a"}
    path = "/v1/reclaim/ownership/cases"
    for changes in (
        {"case_id": "not-a-uuid"},
        {"case_id": "00000000-0000-1000-8000-000000000002"},
        {"schema_version": 2},
        {"cycle": {**body()["cycle"], "domain": "example.com"}},
        {"cycle": {**body()["cycle"], "lead_code": "x" * 257}},
    ):
        response = client.post(path, json={**body(), **changes}, headers=headers)
        assert response.status_code == 422, response.json()


def test_disabled_service_returns_503_after_authentication() -> None:
    client = make_client(enabled=False)
    assert (
        client.post(
            "/v1/reclaim/ownership/cases", json=body(), headers={"X-API-Key": "own-a"}
        ).status_code
        == 503
    )


def test_ownership_has_separate_rate_bucket_and_body_limit() -> None:
    client = make_client()
    client.app.state.rate_limiter = DenyingLimiter()
    limited = client.post(
        "/v1/reclaim/ownership/cases", json=body(), headers={"X-API-Key": "own-a"}
    )
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "22"

    client.app.state.rate_limiter = PermissiveLimiter()
    oversized = client.post(
        "/v1/reclaim/ownership/heartbeat",
        content=b" " * 5000,
        headers={"X-API-Key": "own-a", "Content-Type": "application/json"},
    )
    assert oversized.status_code == 413
