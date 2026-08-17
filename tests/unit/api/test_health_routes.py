from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from ip_proxy_pool.api.routes.health import router
from ip_proxy_pool.observability.metrics import PrometheusMetrics


class Repository:
    def __init__(self, error: Exception | None = None, *, indexes_ready: bool = True) -> None:
        self.error = error
        self.indexes_ready = indexes_ready

    async def ping(self) -> bool:
        if self.error:
            raise self.error
        return True

    async def all_selection_indexes_ready(self) -> bool:
        return self.indexes_ready


def client_for(repository: Any) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.repository = repository
    return TestClient(app, raise_server_exceptions=False)


def test_health_routes_are_anonymous() -> None:
    client = client_for(Repository())

    assert client.get("/health/live").json() == {"status": "alive"}
    assert client.get("/health/ready").json() == {"status": "ready"}


def test_readiness_failure_is_sanitized() -> None:
    client = client_for(Repository(RuntimeError("redis://user:secret@host")))

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert "secret" not in response.text


def test_readiness_fails_when_selection_indexes_have_not_been_built() -> None:
    response = client_for(Repository(indexes_ready=False)).get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "selection indexes not ready"}


def test_metrics_endpoint_exports_prometheus_text() -> None:
    app = FastAPI()
    app.include_router(router)
    registry = CollectorRegistry()
    metrics = PrometheusMetrics(registry=registry)
    metrics.record_probe("success")
    app.state.metrics_registry = registry
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "ip_pool_probe_total" in response.text
