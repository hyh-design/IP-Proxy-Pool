from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ip_proxy_pool.peer_cache.sync import PeerSyncWorker


class Store:
    def __init__(self) -> None:
        self.replacements = []
        self.failures = []

    async def begin_sync(self, owner: str) -> int:
        return 1

    async def replace(self, snapshot, generation, owner, now):
        self.replacements.append(tuple(snapshot))
        from ip_proxy_pool.peer_cache.store import ReplaceResult

        return ReplaceResult(len(snapshot), 0, 0, False)

    async def note_failure(self, outcome: str, now: datetime) -> None:
        self.failures.append(outcome)


def payload(now: datetime, *, scope: str = "formal", origin: str = "system-two") -> dict:
    return {
        "schema_version": 1,
        "origin_node": origin,
        "scope": scope,
        "generated_at": now.isoformat(),
        "items": [
            {
                "endpoint": "8.8.8.8:80",
                "domain": "portal.daqihui.com",
                "score": 95,
                "latency_ewma_ms": 100,
                "last_checked_at": now.isoformat(),
                "consecutive_successes": 3,
                "source_names": ["formal"],
            }
        ],
    }


async def test_valid_formal_snapshot_replaces_cache_without_redirect() -> None:
    now = datetime.now(UTC)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload(now))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    store = Store()
    try:
        worker = PeerSyncWorker(
            store,
            base_url="http://peer-tunnel:8000",
            api_key="peer-secret",
            peer_name="system-two",
            origin_node="system-two",
            domain="portal.daqihui.com",
            client=client,
        )
        result = await worker.sync_once(now)
        assert result.outcome == "success" and result.accepted == 1
        assert len(store.replacements) == 1
        assert seen[0].headers["X-API-Key"] == "peer-secret"
        assert seen[0].url.path == "/v1/peer/proxies"
    finally:
        await client.aclose()


async def test_invalid_scope_and_http_503_never_replace_old_cache() -> None:
    now = datetime.now(UTC)
    responses = [
        httpx.Response(200, json=payload(now, scope="peer")),
        httpx.Response(503, text="unavailable"),
    ]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: responses.pop(0)))
    store = Store()
    try:
        worker = PeerSyncWorker(
            store,
            base_url="http://peer-tunnel:8000",
            api_key="key",
            peer_name="system-two",
            origin_node="system-two",
            domain="portal.daqihui.com",
            client=client,
        )
        assert (await worker.sync_once(now)).outcome == "protocol_error"
        assert (await worker.sync_once(now + timedelta(seconds=60))).outcome == "http_error"
        assert store.replacements == []
        assert store.failures == ["protocol_error", "http_error"]
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (lambda now: httpx.Response(200, json=payload(now, origin="wrong")), "protocol_error"),
        (
            lambda now: httpx.Response(200, json=payload(now + timedelta(seconds=10))),
            "protocol_error",
        ),
        (
            lambda now: httpx.Response(302, headers={"Location": "http://evil.invalid/"}),
            "redirect_error",
        ),
        (lambda now: httpx.Response(200, content=b"x" * (64 * 1024 + 1)), "oversize"),
        (lambda now: httpx.Response(401), "auth_error"),
    ],
)
async def test_protocol_and_http_failures_do_not_replace(response, expected: str) -> None:
    now = datetime.now(UTC)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: response(now)))
    store = Store()
    try:
        worker = PeerSyncWorker(
            store,
            base_url="http://peer-tunnel:8000",
            api_key="key",
            peer_name="system-two",
            origin_node="system-two",
            domain="portal.daqihui.com",
            client=client,
        )
        result = await worker.sync_once(now)
        assert result.outcome == expected
        assert store.replacements == []
    finally:
        await client.aclose()


async def test_valid_empty_snapshot_replaces_with_zero_items() -> None:
    now = datetime.now(UTC)
    empty = payload(now)
    empty["items"] = []
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=empty))
    )
    store = Store()
    try:
        worker = PeerSyncWorker(
            store,
            base_url="http://peer-tunnel:8000",
            api_key="key",
            peer_name="system-two",
            origin_node="system-two",
            domain="portal.daqihui.com",
            client=client,
        )
        result = await worker.sync_once(now)
        assert result.outcome == "success" and result.accepted == 0
        assert store.replacements == [()]
    finally:
        await client.aclose()
