from uuid import uuid4

from fastapi.testclient import TestClient

from ip_proxy_pool.api.app import create_app
from ip_proxy_pool.config import Settings


def settings() -> Settings:
    return Settings.model_validate(
        {
            "api": {
                "api_keys": ["read-key"],
                "admin_api_keys": ["admin-key"],
                "cursor_secret": "x" * 32,
            },
            "reclaim_quota": {
                "enabled": True,
                "api_keys": ["quota-one", "quota-two"],
            },
        }
    )


def make_client() -> TestClient:
    configured = settings()
    app = create_app(configured)
    app.state.settings = configured
    app.state.reclaim_quota_store = object()
    app.state.rate_limiter = object()
    app.state.repository = object()
    return TestClient(app, raise_server_exceptions=False)


def test_quota_route_is_scoped_to_quota_client_keys() -> None:
    client = make_client()
    body = {"schema_version": 1, "domain": "portal.daqihui.com", "attempt_id": str(uuid4())}

    assert client.post("/v1/reclaim/quota/acquire", json=body).status_code == 401
    assert (
        client.post(
            "/v1/reclaim/quota/acquire", json=body, headers={"X-API-Key": "read-key"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/reclaim/quota/acquire", json=body, headers={"X-API-Key": "admin-key"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/reclaim/quota/acquire", json=body, headers={"X-API-Key": "quota-one"}
        ).status_code
        == 503
    )
    assert client.get("/v1/domains", headers={"X-API-Key": "quota-one"}).status_code == 403
    assert (
        client.post(
            "/v1/proxies/feedback",
            json={"domain": "portal.daqihui.com", "endpoint": "1.1.1.1:80"},
            headers={"X-API-Key": "quota-one"},
        ).status_code
        == 403
    )
    assert (
        client.get("/v1/dashboard/summary", headers={"X-API-Key": "quota-one"}).status_code == 403
    )


def test_quota_route_rejects_wrong_domain_and_bad_id() -> None:
    client = make_client()
    headers = {"X-API-Key": "quota-one"}

    assert (
        client.post(
            "/v1/reclaim/quota/acquire",
            json={"schema_version": 1, "domain": "other.example", "attempt_id": str(uuid4())},
            headers=headers,
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/reclaim/quota/acquire",
            json={"schema_version": 1, "domain": "portal.daqihui.com", "attempt_id": "not-uuid"},
            headers=headers,
        ).status_code
        == 422
    )
