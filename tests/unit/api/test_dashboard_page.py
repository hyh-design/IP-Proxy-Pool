from fastapi.testclient import TestClient

from ip_proxy_pool.api.app import create_app
from ip_proxy_pool.config import Settings

EXPECTED_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'none'"
)


def settings(*, enabled: bool = True) -> Settings:
    return Settings.model_validate(
        {
            "api": {
                "api_keys": ["read-key"],
                "cursor_secret": "x" * 32,
            },
            "dashboard": {"enabled": enabled},
        }
    )


def test_dashboard_page_is_self_contained_and_hardened() -> None:
    client = TestClient(create_app(settings()), raise_server_exceptions=False)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert 'src="/dashboard/assets/dashboard.js"' in response.text
    assert "https://" not in response.text
    assert response.headers["content-security-policy"] == EXPECTED_CSP
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_dashboard_assets_have_correct_types_and_cache_policy() -> None:
    client = TestClient(create_app(settings()), raise_server_exceptions=False)

    css = client.get("/dashboard/assets/dashboard.css")
    font = client.get("/dashboard/assets/fonts/IBMPlexSans-Regular.woff2")

    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert css.headers["cache-control"] == "public, max-age=3600"
    assert font.status_code == 200
    assert font.headers["content-type"].startswith("font/woff2")


def test_dashboard_page_and_assets_are_absent_when_disabled() -> None:
    client = TestClient(create_app(settings(enabled=False)), raise_server_exceptions=False)

    assert client.get("/dashboard").status_code == 404
    assert client.get("/dashboard/assets/dashboard.css").status_code == 404
