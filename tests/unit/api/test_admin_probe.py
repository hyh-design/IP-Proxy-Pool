from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ip_proxy_pool.api.routes.admin import build_admin_router
from ip_proxy_pool.config import Settings
from ip_proxy_pool.security.rate_limit import RateLimitDecision

Resolver = Callable[[str], Awaitable[set[str]]]


class Limiter:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def check(self, *args: Any, **kwargs: Any) -> RateLimitDecision:
        del args, kwargs
        if self.error:
            raise self.error
        return RateLimitDecision(True, 10, 9, 0)


def enabled_settings() -> Settings:
    return Settings.model_validate(
        {
            "api": {
                "api_keys": ["read-key"],
                "admin_api_keys": ["admin-key"],
                "cursor_secret": "x" * 32,
                "admin_probe_enabled": True,
            },
            "security": {"allowed_probe_hosts": ["allowed.example"]},
        }
    )


async def public_resolver(host: str) -> set[str]:
    del host
    return {"1.1.1.1"}


def client_for(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    resolver: Resolver = public_resolver,
    limiter: Limiter | None = None,
) -> TestClient:
    app = FastAPI()
    settings = enabled_settings()
    app.include_router(
        build_admin_router(
            settings,
            resolver=resolver,
            transport=httpx.MockTransport(handler),
        )
    )
    app.state.settings = settings
    app.state.rate_limiter = limiter or Limiter()
    return TestClient(app, raise_server_exceptions=False)


def valid_probe() -> dict[str, object]:
    return {
        "proxy": "1.1.1.1:80",
        "url": "https://allowed.example/",
        "timeout_seconds": 5,
    }


def test_admin_probe_rejects_private_dns() -> None:
    async def private_resolver(host: str) -> set[str]:
        del host
        return {"127.0.0.1"}

    client = client_for(
        lambda request: httpx.Response(200, request=request),
        resolver=private_resolver,
    )

    response = client.post(
        "/v1/admin/probe",
        json=valid_probe(),
        headers={"X-API-Key": "admin-key"},
    )

    assert response.status_code == 422


def test_admin_probe_requires_admin_role() -> None:
    client = client_for(lambda request: httpx.Response(200, request=request))

    response = client.post(
        "/v1/admin/probe",
        json=valid_probe(),
        headers={"X-API-Key": "read-key"},
    )

    assert response.status_code == 403


def test_admin_probe_rejects_non_global_proxy() -> None:
    client = client_for(lambda request: httpx.Response(200, request=request))
    body = valid_probe()
    body["proxy"] = "127.0.0.1:80"

    response = client.post(
        "/v1/admin/probe",
        json=body,
        headers={"X-API-Key": "admin-key"},
    )

    assert response.status_code == 422


def test_timeout_is_bounded() -> None:
    client = client_for(lambda request: httpx.Response(200, request=request))

    for timeout in (0, 31):
        body = valid_probe()
        body["timeout_seconds"] = timeout
        response = client.post(
            "/v1/admin/probe",
            json=body,
            headers={"X-API-Key": "admin-key"},
        )
        assert response.status_code == 422


def test_redirect_is_not_followed_and_text_snippet_is_bounded() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"location": "https://allowed.example/next", "content-type": "text/plain"},
            text="x" * 3000,
            request=request,
        )

    client = client_for(handler)

    response = client.post(
        "/v1/admin/probe",
        json=valid_probe(),
        headers={"X-API-Key": "admin-key"},
    )

    assert response.status_code == 200
    assert response.json()["status_code"] == 302
    assert len(response.json()["snippet"]) == 2048
    assert len(requests) == 1


def test_binary_response_has_no_snippet() -> None:
    client = client_for(
        lambda request: httpx.Response(
            200,
            content=b"\x00\x01",
            headers={"content-type": "application/octet-stream"},
            request=request,
        )
    )

    response = client.post(
        "/v1/admin/probe",
        json=valid_probe(),
        headers={"X-API-Key": "admin-key"},
    )

    assert response.json()["snippet"] is None


def test_admin_limiter_storage_failure_fails_closed() -> None:
    client = client_for(
        lambda request: httpx.Response(200, request=request),
        limiter=Limiter(RuntimeError("redis password secret")),
    )

    response = client.post(
        "/v1/admin/probe",
        json=valid_probe(),
        headers={"X-API-Key": "admin-key"},
    )

    assert response.status_code == 503
    assert "secret" not in response.text


def test_probe_system_error_is_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise RuntimeError("https://user:password@internal/")

    client = client_for(handler)

    response = client.post(
        "/v1/admin/probe",
        json=valid_probe(),
        headers={"X-API-Key": "admin-key"},
    )

    assert response.status_code == 200
    assert response.json()["category"] == "system_error"
    assert "password" not in response.text
