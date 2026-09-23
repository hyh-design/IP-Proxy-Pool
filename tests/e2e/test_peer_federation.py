"""Two isolated API namespaces and the real reclaim proxy client."""

import importlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis

from ip_proxy_pool.api.app import create_app
from ip_proxy_pool.config import Settings
from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState
from ip_proxy_pool.peer_cache.store import PeerCacheStore
from ip_proxy_pool.peer_cache.sync import PeerSyncWorker
from ip_proxy_pool.storage.keys import keys_for
from ip_proxy_pool.storage.repository import RedisRepository

DOMAIN = "portal.daqihui.com"


@pytest.fixture
def peer_redis(tmp_path: Path) -> Iterator[str]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    process = subprocess.Popen(
        [
            "redis-server",
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
            "--save",
            "",
            "--appendonly",
            "no",
            "--dir",
            str(tmp_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.02)
        else:
            pytest.fail("isolated Redis did not start")
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        process.terminate()
        process.wait(timeout=5)


def settings(redis_url: str, local: str, peer: str) -> Settings:
    return Settings.model_validate(
        {
            "redis": {"url": redis_url, "key_prefix": f"federation:{local}"},
            "api": {"api_keys": [f"read-{local}"], "cursor_secret": "x" * 32},
            "target": {"domain": DOMAIN},
            "peer_export": {
                "enabled": True,
                "node_id": local,
                "api_keys": [f"export-{local}"],
            },
            "peer_cache": {
                "enabled": True,
                "peer_name": peer,
                "origin_node": peer,
            },
            "peer_alerts": {"enabled": True, "webhook_url": "https://example.invalid/hook"},
            "dashboard": {"enabled": False},
        }
    )


async def seed_formal(redis: Redis, prefix: str, endpoint: str, now: datetime) -> None:
    record = ProxyRecord(
        endpoint=ProxyEndpoint.parse(endpoint),
        domain=DOMAIN,
        score=95,
        state=ProxyState.AVAILABLE,
        source_names={"formal"},
        first_seen_at=now,
        last_seen_at=now,
        last_checked_at=now,
        next_check_at=now + timedelta(minutes=5),
        latency_ewma_ms=100,
        consecutive_successes=3,
    )
    await RedisRepository(redis, prefix=prefix).save_record(record)
    await redis.set(keys_for(prefix, DOMAIN).available_latency_ready, "1")


class ClientAdapter:
    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.trust_env = False

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        return self.client.get(url, **kwargs)

    def post(self, url: str, **kwargs: object) -> httpx.Response:
        return self.client.post(url, **kwargs)

    def close(self) -> None:
        pass


class FirstChoice:
    def choice(self, values: list[object]) -> object:
        return values[0]


async def test_two_way_formal_only_and_real_client_feedback(peer_redis: str) -> None:
    reclaim_root = os.getenv("DAQIHUI_RECLAIM_ROOT")
    if not reclaim_root:
        pytest.skip("set DAQIHUI_RECLAIM_ROOT for cross-repository acceptance")
    root = Path(reclaim_root).resolve()
    if not (root / "daqihui" / "proxy_pool.py").is_file():
        pytest.fail("DAQIHUI_RECLAIM_ROOT does not contain the reclaim client")
    sys.path.insert(0, str(root))
    try:
        proxy_module = importlib.import_module("daqihui.proxy_pool")
    finally:
        sys.path.remove(str(root))

    redis = Redis.from_url(peer_redis, decode_responses=True)
    now = datetime.now(UTC)
    first = settings(peer_redis, "system-one", "system-two")
    second = settings(peer_redis, "system-two", "system-one")
    try:
        await seed_formal(redis, "federation:system-one", "1.1.1.1:80", now)
        await seed_formal(redis, "federation:system-two", "8.8.8.8:80", now)
        with TestClient(create_app(first)) as client_a, TestClient(create_app(second)) as client_b:

            def export_from_b(request: httpx.Request) -> httpx.Response:
                response = client_b.get(
                    request.url.path,
                    params=dict(request.url.params),
                    headers={"X-API-Key": request.headers["X-API-Key"]},
                )
                return httpx.Response(response.status_code, content=response.content)

            transport = httpx.AsyncClient(transport=httpx.MockTransport(export_from_b))
            store_a = PeerCacheStore(
                redis,
                prefix="federation:system-one",
                peer_name="system-two",
                domain=DOMAIN,
                origin_node="system-two",
            )
            try:
                worker = PeerSyncWorker(
                    store_a,
                    base_url="http://peer-tunnel:8000",
                    api_key="export-system-two",
                    peer_name="system-two",
                    origin_node="system-two",
                    domain=DOMAIN,
                    client=transport,
                )
                outcome = await worker.sync_once(datetime.now(UTC))
                assert outcome.outcome == "success" and outcome.accepted == 1
            finally:
                await transport.aclose()

            def export_from_a(request: httpx.Request) -> httpx.Response:
                response = client_a.get(
                    request.url.path,
                    params=dict(request.url.params),
                    headers={"X-API-Key": request.headers["X-API-Key"]},
                )
                return httpx.Response(response.status_code, content=response.content)

            reverse_transport = httpx.AsyncClient(transport=httpx.MockTransport(export_from_a))
            store_b = PeerCacheStore(
                redis,
                prefix="federation:system-two",
                peer_name="system-one",
                domain=DOMAIN,
                origin_node="system-one",
            )
            try:
                reverse_worker = PeerSyncWorker(
                    store_b,
                    base_url="http://peer-tunnel:8000",
                    api_key="export-system-one",
                    peer_name="system-one",
                    origin_node="system-one",
                    domain=DOMAIN,
                    client=reverse_transport,
                )
                reverse = await reverse_worker.sync_once(datetime.now(UTC))
                assert reverse.outcome == "success" and reverse.accepted == 1
            finally:
                await reverse_transport.aclose()

            b_export = client_b.get(
                "/v1/peer/proxies",
                params={"domain": DOMAIN},
                headers={"X-API-Key": "export-system-two"},
            )
            assert b_export.status_code == 200
            assert [item["endpoint"] for item in b_export.json()["items"]] == ["8.8.8.8:80"]

            adapter = ClientAdapter(client_a)
            business = proxy_module.ProxyPoolClient(
                "http://127.0.0.1:8000",
                "read-system-one",
                domain=DOMAIN,
                candidate_count=2,
                peer_cache_enabled=True,
                session=adapter,
                feedback_session_factory=lambda: ClientAdapter(client_a),
                rng=FirstChoice(),
            )
            selected_formal = business.next_selection()
            assert selected_formal.source == "formal"
            business.mark_failed(selected_formal.proxy_url)
            selected_peer = business.next_selection()
            assert selected_peer.source == "peer"
            assert selected_peer.proxy_url == "http://8.8.8.8:80"
            assert business.report_failure(selected_peer.proxy_url, selection=selected_peer)
            assert (
                await redis.hget(keys_for("federation:system-two", DOMAIN).records, "8.8.8.8:80")
                is not None
            )
            a_export = client_a.get(
                "/v1/peer/proxies",
                params={"domain": DOMAIN},
                headers={"X-API-Key": "export-system-one"},
            )
            assert a_export.status_code == 200
            assert [item["endpoint"] for item in a_export.json()["items"]] == ["1.1.1.1:80"]
    finally:
        await redis.aclose()
