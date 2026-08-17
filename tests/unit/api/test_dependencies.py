from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from ip_proxy_pool.api import dependencies
from ip_proxy_pool.config import Settings


class FakeRedis:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def request_for(app: FastAPI) -> Request:
    return Request({"type": "http", "app": app, "headers": []})


def valid_settings() -> Settings:
    return Settings.model_validate(
        {
            "redis": {"url": "redis://localhost:6379/15", "key_prefix": "test"},
            "api": {
                "api_keys": ["read-key"],
                "admin_api_keys": ["admin-key"],
                "cursor_secret": "x" * 32,
            },
        }
    )


def test_missing_application_state_returns_sanitized_503() -> None:
    app = FastAPI()
    request = request_for(app)

    with pytest.raises(HTTPException) as captured:
        dependencies.get_repository(request)

    assert captured.value.status_code == 503
    assert "state" not in str(captured.value.detail).lower()

    with pytest.raises(HTTPException) as dashboard_error:
        dependencies.get_dashboard_service(request)

    assert dashboard_error.value.status_code == 503


async def test_lifespan_creates_one_client_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRedis()
    calls = 0

    def from_url(*args: Any, **kwargs: Any) -> FakeRedis:
        nonlocal calls
        del args, kwargs
        calls += 1
        return fake

    monkeypatch.setattr(dependencies.Redis, "from_url", from_url)
    app = FastAPI(lifespan=dependencies.build_lifespan(valid_settings()))

    async with app.router.lifespan_context(app):
        assert calls == 1
        assert app.state.repository is not None
        assert app.state.rate_limiter is not None
        assert app.state.cursor_codec is not None
        assert app.state.dashboard_service is not None
        assert (
            app.state.repository._priority_max_latency_ms
            == app.state.settings.selection.max_latency_ms
        )
        assert fake.closed is False

    assert fake.closed is True
