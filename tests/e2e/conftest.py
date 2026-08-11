import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import Page, expect
from redis import Redis

from ip_proxy_pool.dashboard.keys import history_keys
from ip_proxy_pool.dashboard.models import (
    HistoryPoint,
    HistoryResolution,
    LatencySummary,
    ScoreBuckets,
)
from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState
from ip_proxy_pool.storage.codec import encode_record
from ip_proxy_pool.storage.keys import keys_for


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _seed(redis_url: str, prefix: str) -> None:
    redis = Redis.from_url(redis_url, decode_responses=True)
    redis.flushdb()
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    records = (
        ProxyRecord(
            endpoint=ProxyEndpoint.parse("1.1.1.1:80"),
            domain="example.com",
            score=95,
            state=ProxyState.AVAILABLE,
            source_names={"alpha"},
            first_seen_at=now - timedelta(hours=1),
            last_seen_at=now,
            last_checked_at=now,
            next_check_at=now + timedelta(minutes=5),
            consecutive_successes=3,
            latency_ewma_ms=90,
        ),
        ProxyRecord(
            endpoint=ProxyEndpoint.parse("2.2.2.2:8080"),
            domain="example.com",
            score=72,
            state=ProxyState.DEGRADED,
            source_names={"beta"},
            first_seen_at=now - timedelta(hours=1),
            last_seen_at=now,
            last_checked_at=now,
            next_check_at=now + timedelta(minutes=5),
            consecutive_successes=0,
            latency_ewma_ms=240,
        ),
    )
    for record in records:
        keys = keys_for(prefix, record.domain)
        pipeline = redis.pipeline(transaction=True)
        pipeline.sadd(f"{prefix}:domains", record.domain)
        pipeline.hset(keys.records, record.endpoint.canonical, encode_record(record))
        pipeline.zadd(keys.quality, {record.endpoint.canonical: record.score})
        pipeline.zadd(keys.due, {record.endpoint.canonical: record.next_check_at.timestamp()})
        pipeline.execute()

    for minutes in (15, 5):
        observed_at = (now - timedelta(minutes=minutes)).replace(
            minute=((now - timedelta(minutes=minutes)).minute // 5) * 5
        )
        point = HistoryPoint(
            observed_at=observed_at,
            total=2,
            candidate=0,
            available=1,
            degraded=1,
            quarantined=0,
            due=2,
            leased=0,
            availability_rate=0.5,
            available_pool_share=0.5,
            high_quality=1,
            score_buckets=ScoreBuckets(watch=1, high=1),
            latency=LatencySummary(
                samples=2,
                average_ms=165,
                p50_ms=90,
                p95_ms=240,
            ),
        )
        keys = history_keys(prefix, "example.com", HistoryResolution.FIVE_MINUTES)
        bucket = observed_at.strftime("%Y%m%d%H%M")
        redis.hset(keys.data, bucket, point.model_dump_json())
        redis.zadd(keys.index, {bucket: observed_at.timestamp()})
    for role in ("collector", "checker"):
        instance = f"{role}-e2e"
        redis.set(f"{prefix}:worker-heartbeat:{role}:{instance}", now.timestamp(), ex=90)
        redis.zadd(f"{prefix}:worker-heartbeat:index:{role}", {instance: now.timestamp()})
    redis.close()


@pytest.fixture(scope="session")
def dashboard_url() -> Iterator[str]:
    redis_url = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")
    prefix = "ippool:e2e"
    _seed(redis_url, prefix)
    port = _free_port()
    env = {
        **os.environ,
        "IP_POOL_REDIS__URL": redis_url,
        "IP_POOL_REDIS__KEY_PREFIX": prefix,
        "IP_POOL_API__API_KEYS": '["read-key"]',
        "IP_POOL_API__CURSOR_SECRET": "x" * 32,
        "IP_POOL_DASHBOARD__ENABLED": "true",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "ip_proxy_pool.api.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/health/ready", timeout=0.5) as response:
                if response.status == 200:
                    break
        except OSError:
            time.sleep(0.1)
    else:
        process.terminate()
        process.wait(timeout=5)
        pytest.fail("dashboard test server did not become ready")
    try:
        yield f"{base}/dashboard"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.fixture
def authenticated_page(page: Page, dashboard_url: str) -> Page:
    page.goto(dashboard_url)
    page.get_by_label("API Key").fill("read-key")
    page.get_by_role("button", name="进入仪表盘").click()
    expect(page.locator("#dashboard-content")).to_be_visible()
    return page
