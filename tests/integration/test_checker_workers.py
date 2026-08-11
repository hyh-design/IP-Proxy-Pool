import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from redis.asyncio import Redis

from ip_proxy_pool.checker.circuit_breaker import RedisCircuitBreaker
from ip_proxy_pool.checker.worker import CheckerWorker
from ip_proxy_pool.config import CheckerSettings
from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord
from ip_proxy_pool.storage.repository import RedisRepository


class SuccessfulClient:
    async def request(self, endpoint: ProxyEndpoint | None, target: object) -> httpx.Response:
        del endpoint, target
        return httpx.Response(200, json={"origin": "1.1.1.1"})


class BlockingClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def request(self, endpoint: ProxyEndpoint | None, target: object) -> httpx.Response:
        del target
        if endpoint is not None:
            self.started.set()
            await self.release.wait()
        return httpx.Response(200, json={"origin": "1.1.1.1"})


def make_record(number: int) -> ProxyRecord:
    observed = datetime.fromtimestamp(1, UTC)
    return ProxyRecord(
        endpoint=ProxyEndpoint.parse(f"1.1.1.{number}:80"),
        domain="example.com",
        source_names={"integration"},
        first_seen_at=observed,
        last_seen_at=observed,
        next_check_at=observed,
    )


def make_worker(
    redis_client: Redis,
    worker_id: str,
    *,
    probe_client: SuccessfulClient | BlockingClient | None = None,
) -> CheckerWorker:
    return CheckerWorker(
        repository=RedisRepository(redis_client, prefix="ippool:test"),
        breaker=RedisCircuitBreaker(redis_client, prefix="ippool:test", failures=3, cooldown=60),
        probe_client=probe_client or SuccessfulClient(),
        targets={
            "example.com": {
                "name": "example",
                "url": "https://example.com/ip",
                "domain": "example.com",
                "json_keys": ("origin",),
            }
        },
        settings=CheckerSettings(concurrency=3, batch_size=10, lease_seconds=60),
        worker_id=worker_id,
    )


@pytest.mark.docker
async def test_two_workers_complete_each_endpoint_once(redis_client: Redis) -> None:
    repository = RedisRepository(redis_client, prefix="ippool:test")
    records = [make_record(number) for number in range(1, 11)]
    for record in records:
        await repository.upsert_candidate(record)
    first = make_worker(redis_client, "worker-1")
    second = make_worker(redis_client, "worker-2")

    first_run, second_run = await asyncio.gather(
        first.run_once("example.com", now=100.0),
        second.run_once("example.com", now=100.0),
    )
    confirmation_runs = await asyncio.gather(
        first.run_once("example.com", now=200.0),
        second.run_once("example.com", now=200.0),
    )

    assert first_run.completed + second_run.completed == 10
    assert sum(run.completed for run in confirmation_runs) == 10
    for original in records:
        stored = await repository.get_record("example.com", original.endpoint.canonical)
        assert stored is not None
        assert stored.success_count == 2
    stats = await repository.stats("example.com")
    assert stats.available == 10
    assert stats.leased == 0


@pytest.mark.docker
async def test_worker_cancellation_releases_claimed_tasks(redis_client: Redis) -> None:
    repository = RedisRepository(redis_client, prefix="ippool:test")
    original = make_record(1)
    await repository.upsert_candidate(original)
    client = BlockingClient()
    worker = make_worker(redis_client, "worker-cancelled", probe_client=client)
    task = asyncio.create_task(worker.run_once("example.com", now=100.0))
    await asyncio.wait_for(client.started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    stored = await repository.get_record("example.com", original.endpoint.canonical)
    stats = await repository.stats("example.com")
    assert stored is not None
    assert stored.score == original.score
    assert stored.failure_count == 0
    assert stats.leased == 0
    assert stats.due == 1
