"""Real Redis/API and the business ownership client share a versioned contract."""

import importlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis

from ip_proxy_pool.api.app import create_app
from ip_proxy_pool.config import Settings


class ClientAdapter:
    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.trust_env = False

    def get(self, url: str, **kwargs: object):
        return self.client.get(url, **kwargs)

    def post(self, url: str, **kwargs: object):
        return self.client.post(url, **kwargs)

    def close(self) -> None:
        pass


class Clock:
    def __init__(self) -> None:
        self.value = int(datetime.now(UTC).timestamp() * 1000)

    def __call__(self) -> int:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += seconds * 1000


@pytest.fixture
def isolated_redis(tmp_path: Path) -> Iterator[str]:
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


def business_module():
    reclaim_root = os.getenv("DAQIHUI_RECLAIM_ROOT")
    if not reclaim_root:
        pytest.skip("set DAQIHUI_RECLAIM_ROOT for cross-repository acceptance")
    root = Path(reclaim_root).resolve()
    if not (root / "daqihui" / "ownership_client.py").is_file():
        pytest.fail("DAQIHUI_RECLAIM_ROOT does not contain the ownership client")
    sys.path.insert(0, str(root))
    try:
        return importlib.import_module("daqihui.ownership_client")
    finally:
        sys.path.remove(str(root))


async def test_two_business_clients_reconcile_without_using_quota(isolated_redis: str) -> None:
    module = business_module()
    prefix = "ownership-acceptance"
    settings = Settings.model_validate(
        {
            "redis": {"url": isolated_redis, "key_prefix": prefix},
            "api": {"api_keys": ["read-key"], "cursor_secret": "x" * 32},
            "reclaim_quota": {"api_keys": ["quota-key"]},
            "ownership": {
                "enabled": True,
                "members": {
                    "one:a": {"system_id": "system-one", "api_key": "own-a"},
                    "two:b": {"system_id": "system-two", "api_key": "own-b"},
                },
            },
        }
    )
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    quota_key = f"{prefix}:reclaim:global-quota:portal.daqihui.com:window"
    await redis.zadd(quota_key, {"prior-attempt": 1})
    quota_before = await redis.zrange(quota_key, 0, -1, withscores=True)
    app_one, app_two = create_app(settings), create_app(settings)
    clock = Clock()
    try:
        with TestClient(app_one) as api_one, TestClient(app_two) as api_two:
            app_one.state.ownership_store._clock_ms = clock
            app_two.state.ownership_store._clock_ms = clock
            one = module.OwnershipClient(
                "http://127.0.0.1:8000", "own-a", "one:a", session=ClientAdapter(api_one)
            )
            two = module.OwnershipClient(
                "http://127.0.0.1:8000", "own-b", "two:b", session=ClientAdapter(api_two)
            )

            def cycle(code: str, record_id: int):
                return module.OwnershipCycle(
                    "portal.daqihui.com", code, record_id, "2026-09-23 08:00:00"
                )

            def check(client, case_id: str, expected_round: int, result: str) -> str:
                job = client.claim_job()
                assert job is not None and job.case_id == case_id
                assert job.round_no == expected_round
                return client.submit_result(
                    case_id, expected_round, result, str(uuid4()), job.lease_token
                ).status

            first_case, second_case = str(uuid4()), str(uuid4())
            assert one.open_case(first_case, cycle("L-101", 101)).status == "pending"
            # Finish one case with explicit absence from both members in two rounds.
            assert check(one, first_case, 1, "absent") == "pending"
            assert check(two, first_case, 1, "absent") == "pending"
            assert one.read_case(first_case).status == "pending"
            clock.advance(30)
            assert check(one, first_case, 2, "absent") == "pending"
            assert check(two, first_case, 2, "absent") == "external"
            assert one.read_case(first_case).revision == 1
            event_id = str(uuid4())
            receipt = two.publish_success(event_id, cycle("L-101", 101))
            assert two.publish_success(event_id, cycle("L-101", 101)) == receipt
            corrected = one.read_case(first_case)
            assert (corrected.status, corrected.revision) == ("internal", 2)
            restarted = module.OwnershipClient(
                "http://127.0.0.1:8000", "own-b", "two:b", session=ClientAdapter(api_two)
            )
            assert restarted.publish_success(event_id, cycle("L-101", 101)) == receipt

            # One positive is enough; a missing member is never counted as absent.
            clock.advance(1)
            assert two.open_case(second_case, cycle("L-102", 102)).status == "pending"
            assert check(one, second_case, 1, "absent") == "pending"
            assert two.read_case(second_case).status == "pending"
            assert check(two, second_case, 1, "found") == "internal"
            missing_case = one.open_case(str(uuid4()), cycle("L-103", 103))
            assert check(one, missing_case.case_id, 1, "absent") == "pending"
            clock.advance(30)
            assert one.read_case(missing_case.case_id).status == "pending"
            assert (
                api_one.get(
                    f"/v1/reclaim/ownership/cases/{first_case}",
                    params={"schema_version": 1},
                    headers={"X-API-Key": "quota-key"},
                ).status_code
                == 403
            )
            assert (
                api_one.post(
                    "/v1/reclaim/quota/acquire",
                    json={
                        "schema_version": 1,
                        "domain": "portal.daqihui.com",
                        "attempt_id": str(uuid4()),
                    },
                    headers={"X-API-Key": "own-a"},
                ).status_code
                == 403
            )
            # Two simultaneous case creations remain isolated across API instances.
            clock.advance(1)
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(one.open_case, str(uuid4()), cycle("L-104", 104))
                second = executor.submit(two.open_case, str(uuid4()), cycle("L-105", 105))
                assert first.result().status == "pending"
                assert second.result().status == "pending"
        assert await redis.zrange(quota_key, 0, -1, withscores=True) == quota_before
    finally:
        await redis.aclose()
