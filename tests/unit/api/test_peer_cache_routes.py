from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ip_proxy_pool.api.routes.proxies import router
from ip_proxy_pool.config import Settings
from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState
from ip_proxy_pool.observability.metrics import NoopMetrics
from ip_proxy_pool.peer_cache.receipts import InvalidReceipt, ReceiptBindingError, SelectionReceipt
from ip_proxy_pool.peer_cache.store import SelectedProxy
from ip_proxy_pool.security.rate_limit import RateLimitDecision
from ip_proxy_pool.storage.repository import ProxySelection


class Limiter:
    async def check(self, *args: Any, **kwargs: Any) -> RateLimitDecision:
        return RateLimitDecision(True, 120, 119, 0)


class Repository:
    def __init__(self, now: datetime) -> None:
        self.formal = ProxyRecord(
            endpoint=ProxyEndpoint.parse("1.1.1.1:80"),
            domain="portal.daqihui.com",
            score=95,
            state=ProxyState.AVAILABLE,
            source_names={"formal"},
            first_seen_at=now,
            last_seen_at=now,
            last_checked_at=now,
            next_check_at=now,
            consecutive_successes=3,
            latency_ewma_ms=100,
        )
        self.saved: ProxyRecord | None = None
        self.get_calls = 0

    async def list_domains(self) -> list[str]:
        return ["portal.daqihui.com"]

    async def select_random_proxies(self, **kwargs: Any) -> ProxySelection:
        return ProxySelection((self.formal,), 1, 1, 1, 0, 0, 0, 0)

    async def get_record(self, domain: str, endpoint: str) -> ProxyRecord | None:
        self.get_calls += 1
        return self.formal

    async def save_record(self, record: ProxyRecord) -> None:
        self.saved = record
        self.formal = record


class Cache:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.calls = 0
        self.invalidated = False
        self.failure: Exception | None = None

    async def select(
        self,
        policy: Any,
        count: int,
        exclusions: tuple[str, ...],
        principal_fingerprint: str,
        now: datetime,
    ) -> tuple[SelectedProxy, ...]:
        self.calls += 1
        if self.failure:
            raise self.failure
        assert exclusions == ("1.1.1.1:80",)
        return tuple(
            SelectedProxy(
                endpoint=f"8.8.8.{number}:80",
                domain="portal.daqihui.com",
                score=95,
                latency_ewma_ms=100,
                last_checked_at=now,
                consecutive_successes=3,
                source_names=("peer-formal",),
                selection_source="peer",
                peer_name="system-two",
                selection_token=f"peer-token-{number}",
                usable_until=now + timedelta(seconds=175),
            )
            for number in range(1, min(count, 2) + 1)
        )

    async def invalidate(self, receipt: SelectionReceipt, now: datetime) -> bool:
        self.invalidated = True
        return True


class Receipts:
    def __init__(self) -> None:
        self.formal_claims = 0

    async def issue(self, *args: Any, **kwargs: Any) -> str:
        return "formal-token"

    async def resolve(
        self, token: str, principal_fingerprint: str, domain: str, endpoint: str, now: datetime
    ) -> SelectionReceipt:
        if token == "unknown":
            raise InvalidReceipt("invalid")
        if token == "wrong-binding":
            raise ReceiptBindingError("mismatch")
        return SelectionReceipt(
            digest="a" * 64,
            principal_fingerprint=principal_fingerprint,
            domain=domain,
            endpoint=endpoint,
            selection_source="formal" if token == "formal-token" else "peer",
            peer_name=None if token == "formal-token" else "system-two",
            source_checked_at=now.timestamp(),
            snapshot={
                "endpoint": endpoint,
                "domain": domain,
                "score": 95,
                "latency_ewma_ms": 100,
                "last_checked_at": now.isoformat(),
                "consecutive_successes": 3,
                "source_names": ["peer-formal"],
            },
            issued_at=now.timestamp(),
            expires_at=now.timestamp() + 600,
        )

    async def claim_formal_failure(self, receipt: SelectionReceipt) -> bool:
        self.formal_claims += 1
        return self.formal_claims == 1


def make_client(*, enabled: bool = True) -> tuple[TestClient, Repository, Cache]:
    now = datetime.now(UTC)
    settings = Settings.model_validate(
        {
            "api": {"api_keys": ["read"], "cursor_secret": "x" * 32},
            "target": {"domain": "portal.daqihui.com"},
            "peer_export": {"node_id": "system-one"},
            "peer_cache": {
                "enabled": enabled,
                "peer_name": "system-two",
                "origin_node": "system-two",
                "base_url": "http://127.0.0.1:18000",
                "api_key": "remote-peer-key",
            },
        }
    )
    app = FastAPI()
    app.include_router(router)
    repo, cache = Repository(now), Cache(now)
    app.state.settings = settings
    app.state.repository = repo
    app.state.rate_limiter = Limiter()
    app.state.metrics = NoopMetrics()
    app.state.peer_cache_store = cache
    app.state.selection_receipts = Receipts()
    return TestClient(app, raise_server_exceptions=False), repo, cache


def test_old_request_remains_formal_and_does_not_touch_cache() -> None:
    api, _, cache = make_client()
    response = api.get(
        "/v1/proxies/random",
        params={"domain": "portal.daqihui.com", "count": 3},
        headers={"X-API-Key": "read"},
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert "selection_source" not in response.json()["items"][0]
    assert cache.calls == 0


def test_explicit_cache_mode_appends_peer_after_formal() -> None:
    api, _, cache = make_client()
    response = api.get(
        "/v1/proxies/random",
        params={"domain": "portal.daqihui.com", "count": 3, "include_peer_cache": True},
        headers={"X-API-Key": "read"},
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["selection_source"] for item in items] == ["formal", "peer", "peer"]
    assert items[1]["state"] is None and items[1]["next_check_at"] is None
    assert items[1]["selection_token"] == "peer-token-1"
    assert cache.calls == 1


def test_full_formal_result_and_disabled_switch_do_not_select_peer() -> None:
    api, _, cache = make_client()
    full = api.get(
        "/v1/proxies/random",
        params={"domain": "portal.daqihui.com", "count": 1, "include_peer_cache": True},
        headers={"X-API-Key": "read"},
    )
    assert full.status_code == 200
    assert full.json()["items"][0]["selection_source"] == "formal"
    assert cache.calls == 0
    disabled, _, disabled_cache = make_client(enabled=False)
    response = disabled.get(
        "/v1/proxies/random",
        params={"domain": "portal.daqihui.com", "count": 3, "include_peer_cache": True},
        headers={"X-API-Key": "read"},
    )
    assert response.status_code == 200
    assert "selection_source" not in response.json()["items"][0]
    assert disabled_cache.calls == 0


def test_peer_feedback_uses_receipt_even_if_formal_record_now_exists() -> None:
    api, repo, cache = make_client()
    response = api.post(
        "/v1/proxies/feedback",
        headers={"X-API-Key": "read"},
        json={
            "domain": "portal.daqihui.com",
            "endpoint": "8.8.8.8:80",
            "outcome": "proxy_error",
            "selection_token": "peer-token",
        },
    )
    assert response.status_code == 200
    assert cache.invalidated is True
    assert repo.get_calls == 0 and repo.saved is None
    assert response.json()["state"] is None


def test_bad_receipt_never_falls_back_to_formal_feedback() -> None:
    api, repo, _ = make_client()
    for token, status in (("unknown", 409), ("wrong-binding", 403)):
        response = api.post(
            "/v1/proxies/feedback",
            headers={"X-API-Key": "read"},
            json={
                "domain": "portal.daqihui.com",
                "endpoint": "1.1.1.1:80",
                "outcome": "proxy_error",
                "selection_token": token,
            },
        )
        assert response.status_code == status
    assert repo.get_calls == 0 and repo.saved is None


def test_cross_domain_receipt_is_forbidden_before_domain_lookup() -> None:
    api, repo, _ = make_client()
    response = api.post(
        "/v1/proxies/feedback",
        headers={"X-API-Key": "read"},
        json={
            "domain": "wrong.example",
            "endpoint": "1.1.1.1:80",
            "outcome": "proxy_error",
            "selection_token": "wrong-binding",
        },
    )
    assert response.status_code == 403
    assert repo.get_calls == 0


def test_cache_failure_returns_formal_result() -> None:
    api, _, cache = make_client()
    cache.failure = RuntimeError("cache unavailable")
    response = api.get(
        "/v1/proxies/random",
        params={"domain": "portal.daqihui.com", "count": 3, "include_peer_cache": True},
        headers={"X-API-Key": "read"},
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["endpoint"] == "1.1.1.1:80"


def test_peer_receipt_feedback_still_works_when_cache_switch_is_off() -> None:
    api, repo, cache = make_client(enabled=False)
    response = api.post(
        "/v1/proxies/feedback",
        headers={"X-API-Key": "read"},
        json={
            "domain": "portal.daqihui.com",
            "endpoint": "8.8.8.8:80",
            "outcome": "proxy_error",
            "selection_token": "peer-token",
        },
    )
    assert response.status_code == 200
    assert cache.invalidated is True
    assert repo.saved is None


def test_formal_receipt_failure_is_applied_once() -> None:
    api, repo, _ = make_client()
    payload = {
        "domain": "portal.daqihui.com",
        "endpoint": "1.1.1.1:80",
        "outcome": "proxy_error",
        "selection_token": "formal-token",
    }
    first = api.post("/v1/proxies/feedback", headers={"X-API-Key": "read"}, json=payload)
    assert first.status_code == 200
    saved = repo.saved
    second = api.post("/v1/proxies/feedback", headers={"X-API-Key": "read"}, json=payload)
    assert second.status_code == 200
    assert repo.saved is saved
