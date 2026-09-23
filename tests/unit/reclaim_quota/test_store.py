import asyncio
import importlib
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis

from ip_proxy_pool.api.app import create_app
from ip_proxy_pool.config import Settings


@pytest.fixture
def isolated_redis(tmp_path: Path) -> Iterator[str]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
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
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
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


async def test_two_clients_share_five_grants_and_retry_is_idempotent(
    isolated_redis: str,
) -> None:
    store_module = importlib.import_module("ip_proxy_pool.reclaim_quota.store")
    clients = [Redis.from_url(isolated_redis, decode_responses=True) for _ in range(2)]
    try:
        stores = [
            store_module.ReclaimQuotaStore(
                client,
                prefix="quota-test",
                minimum_redis_uptime_seconds=0,
            )
            for client in clients
        ]
        ids = [str(uuid4()) for _ in range(6)]
        decisions = await asyncio.gather(
            *(
                stores[index % 2].acquire("portal.daqihui.com", attempt_id)
                for index, attempt_id in enumerate(ids)
            )
        )

        assert sum(decision.granted for decision in decisions) == 5
        assert sum(not decision.granted for decision in decisions) == 1
        rejected = next(decision for decision in decisions if not decision.granted)
        assert 0 < rejected.retry_after_ms <= 60_000
        granted_id = ids[
            next(index for index, decision in enumerate(decisions) if decision.granted)
        ]
        duplicate = await stores[1].acquire("portal.daqihui.com", granted_id)
        assert duplicate.granted is True
        assert (
            await clients[0].zcard("quota-test:reclaim:global-quota:portal.daqihui.com:window") == 5
        )
        attempt_key = f"quota-test:reclaim:global-quota:portal.daqihui.com:attempt:{granted_id}"
        assert await clients[0].pttl(attempt_key) > 590_000
    finally:
        for client in clients:
            await client.aclose()


async def test_new_redis_instance_refuses_to_grant_during_restart_grace(
    isolated_redis: str,
) -> None:
    store_module = importlib.import_module("ip_proxy_pool.reclaim_quota.store")
    client = Redis.from_url(isolated_redis, decode_responses=True)
    try:
        store = store_module.ReclaimQuotaStore(client, prefix="quota-test")
        decision = await store.acquire("portal.daqihui.com", str(uuid4()))
        assert decision.granted is False
        assert decision.unavailable is True
        assert await client.keys("quota-test:reclaim:global-quota:*") == []
    finally:
        await client.aclose()


async def test_rolling_window_boundary_is_exact(isolated_redis: str) -> None:
    script_module = importlib.import_module("ip_proxy_pool.reclaim_quota.lua")
    fixed_clock_script = script_module.ACQUIRE.replace(
        "local clock = redis.call('TIME')", "local clock = {ARGV[3], ARGV[4]}"
    )
    client = Redis.from_url(isolated_redis, decode_responses=True)
    base = "quota-test:reclaim:global-quota:portal.daqihui.com"

    async def acquire(attempt_id: str, at_ms: int) -> list[int]:
        result = await client.eval(
            fixed_clock_script,
            2,
            f"{base}:window",
            f"{base}:attempt:{attempt_id}",
            attempt_id,
            "0",
            str(at_ms // 1000),
            str((at_ms % 1000) * 1000),
        )
        return [int(value) for value in result]

    try:
        for _ in range(5):
            assert (await acquire(str(uuid4()), 1_000_000))[0] == 1
        assert (await acquire(str(uuid4()), 1_059_999))[:2] == [0, 1]
        assert (await acquire(str(uuid4()), 1_060_000))[0] == 1
    finally:
        await client.aclose()


def test_live_api_uses_same_redis_authority(isolated_redis: str) -> None:
    store_module = importlib.import_module("ip_proxy_pool.reclaim_quota.store")
    configured = Settings.model_validate(
        {
            "redis": {"url": isolated_redis, "key_prefix": "quota-test-api"},
            "api": {"api_keys": ["read-key"], "cursor_secret": "x" * 32},
            "reclaim_quota": {"enabled": True, "api_keys": ["quota-one", "quota-two"]},
        }
    )
    with (
        TestClient(create_app(configured), raise_server_exceptions=False) as first,
        TestClient(create_app(configured), raise_server_exceptions=False) as second,
    ):
        for client in (first, second):
            client.app.state.reclaim_quota_store = store_module.ReclaimQuotaStore(
                client.app.state.redis,
                prefix="quota-test-api",
                minimum_redis_uptime_seconds=0,
            )
        responses = [
            (first if index % 2 else second).post(
                "/v1/reclaim/quota/acquire",
                json={
                    "schema_version": 1,
                    "domain": "portal.daqihui.com",
                    "attempt_id": str(uuid4()),
                },
                headers={"X-API-Key": "quota-one" if index % 2 else "quota-two"},
            )
            for index in range(6)
        ]
        assert [response.status_code for response in responses] == [200] * 6
        assert sum(response.json()["granted"] for response in responses) == 5
