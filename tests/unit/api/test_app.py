from fastapi.testclient import TestClient

from ip_proxy_pool.api.app import create_app
from ip_proxy_pool.config import Settings


def settings(**api_overrides: object) -> Settings:
    api: dict[str, object] = {
        "api_keys": ["read-key"],
        "admin_api_keys": ["admin-key"],
        "cursor_secret": "x" * 32,
    }
    api.update(api_overrides)
    return Settings.model_validate({"api": api})


def test_app_factory_mounts_versioned_routes() -> None:
    paths = set(create_app(settings()).openapi()["paths"])

    assert "/v1/proxies" in paths
    assert "/v1/proxies/random" in paths
    assert "/v1/stats" in paths
    assert "/health/live" in paths
    assert "/v1/dashboard/summary" in paths
    assert "/v1/dashboard/history" in paths
    assert "/v1/dashboard/quality" in paths
    assert "/v1/dashboard/sources" in paths


def test_dashboard_routes_are_not_mounted_when_disabled() -> None:
    configured = settings()
    configured.dashboard.enabled = False

    paths = set(create_app(configured).openapi()["paths"])

    assert not any(path.startswith("/v1/dashboard/") for path in paths)


def test_admin_probe_is_not_mounted_by_default() -> None:
    paths = set(create_app(settings()).openapi()["paths"])

    assert "/v1/admin/probe" not in paths


def test_admin_probe_is_mounted_only_when_enabled() -> None:
    configured = settings(admin_probe_enabled=True)
    configured.security.allowed_probe_hosts = ("allowed.example",)

    paths = set(create_app(configured).openapi()["paths"])

    assert "/v1/admin/probe" in paths


def test_request_id_is_returned() -> None:
    client = TestClient(create_app(settings()), raise_server_exceptions=False)

    response = client.get("/health/live", headers={"X-Request-ID": "request-123"})

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "request-123"
