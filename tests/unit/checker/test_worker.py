import asyncio
from datetime import UTC, datetime

import fakeredis.aioredis
import httpx

from ip_proxy_pool.checker.circuit_breaker import RedisCircuitBreaker
from ip_proxy_pool.checker.worker import CheckerWorker
from ip_proxy_pool.config import CheckerSettings
from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord
from ip_proxy_pool.models import TestTarget as Target
from ip_proxy_pool.storage.repository import RedisRepository


class StubClient:
    def __init__(
        self,
        *,
        baseline_error: BaseException | None = None,
        proxy_error: BaseException | None = None,
        delay: float = 0,
        proxy_errors_by_target: dict[str, BaseException] | None = None,
    ) -> None:
        self.baseline_error = baseline_error
        self.proxy_error = proxy_error
        self.delay = delay
        self.proxy_errors_by_target = proxy_errors_by_target or {}
        self.target_names: list[str] = []
        self.active = 0
        self.max_active = 0

    async def request(self, endpoint: ProxyEndpoint | None, target: Target) -> httpx.Response:
        target_name = target.name
        self.target_names.append(target_name)
        error = self.baseline_error if endpoint is None else self.proxy_error
        if endpoint is not None:
            error = self.proxy_errors_by_target.get(target_name, error)
        if error is not None:
            raise error
        if endpoint is not None:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(self.delay)
            finally:
                self.active -= 1
        return httpx.Response(200, json={"origin": "1.1.1.1"})


def make_record(address: str = "1.1.1.1:80") -> ProxyRecord:
    observed = datetime.fromtimestamp(1, UTC)
    return ProxyRecord(
        endpoint=ProxyEndpoint.parse(address),
        domain="example.com",
        source_names={"worker-test"},
        first_seen_at=observed,
        last_seen_at=observed,
        next_check_at=observed,
    )


def make_worker(
    client: fakeredis.aioredis.FakeRedis,
    probe_client: StubClient,
    *,
    concurrency: int = 2,
    validation_targets: dict[str, tuple[dict[str, object], ...]] | None = None,
    blocked_proxy_networks: tuple[str, ...] = (),
) -> tuple[CheckerWorker, RedisRepository]:
    repository = RedisRepository(client, prefix="ippool:test")
    breaker = RedisCircuitBreaker(client, prefix="ippool:test", failures=3, cooldown=60)
    worker = CheckerWorker(
        repository=repository,
        breaker=breaker,
        probe_client=probe_client,
        targets={
            "example.com": {
                "name": "example",
                "url": "https://example.com/ip",
                "domain": "example.com",
                "json_keys": ("origin",),
            }
        },
        validation_targets=validation_targets,
        blocked_proxy_networks=blocked_proxy_networks,
        settings=CheckerSettings(
            concurrency=concurrency,
            batch_size=20,
            lease_seconds=60,
        ),
        worker_id="unit-worker",
    )
    return worker, repository


def secondary_target() -> dict[str, object]:
    return {
        "name": "secondary",
        "url": "https://secondary.example/ip",
        "domain": "example.com",
        "json_keys": ("origin",),
    }


async def test_baseline_system_failure_does_not_claim_or_score() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    worker, repo = make_worker(
        client, StubClient(baseline_error=RuntimeError("target unavailable"))
    )
    record = make_record()
    try:
        await repo.upsert_candidate(record)
        before = await repo.get_record("example.com", record.endpoint.canonical)

        run = await worker.run_once("example.com", now=100.0)
        after = await repo.get_record("example.com", record.endpoint.canonical)

        assert run.claimed == 0
        assert run.released == 0
        assert after is not None and before is not None
        assert after.score == before.score
        assert after.failure_count == before.failure_count
        assert await repo.is_due("example.com", record.endpoint.canonical)
    finally:
        await client.aclose()


async def test_proxy_system_error_releases_without_penalty() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    worker, repo = make_worker(client, StubClient(proxy_error=RuntimeError("local TLS failure")))
    record = make_record()
    try:
        await repo.upsert_candidate(record)

        run = await worker.run_once("example.com", now=100.0)
        stored = await repo.get_record("example.com", record.endpoint.canonical)

        assert run.claimed == 1
        assert run.released == 1
        assert run.completed == 0
        assert stored is not None
        assert stored.score == record.score
        assert stored.failure_count == 0
        assert await repo.is_due("example.com", record.endpoint.canonical)
    finally:
        await client.aclose()


async def test_worker_completes_successes_with_bounded_concurrency() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    probe_client = StubClient(delay=0.01)
    worker, repo = make_worker(client, probe_client, concurrency=2)
    try:
        for number in range(5):
            await repo.upsert_candidate(make_record(f"1.1.1.{number + 1}:80"))

        first_run = await worker.run_once("example.com", now=100.0)
        run = await worker.run_once("example.com", now=200.0)
        stats = await repo.stats("example.com")

        assert first_run.claimed == 5
        assert run.claimed == 5
        assert run.completed == 5
        assert run.lost_leases == 0
        assert probe_client.max_active == 2
        assert stats.available == 5
        assert stats.leased == 0
    finally:
        await client.aclose()


async def test_worker_requires_secondary_target_before_scoring_success() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    probe_client = StubClient(
        proxy_errors_by_target={"secondary": httpx.ProxyError("secondary rejected")}
    )
    worker, repo = make_worker(
        client,
        probe_client,
        validation_targets={"example.com": (secondary_target(),)},
    )
    record = make_record()
    try:
        await repo.upsert_candidate(record)

        run = await worker.run_once("example.com", now=100.0)
        stored = await repo.get_record("example.com", record.endpoint.canonical)

        assert run.completed == 1
        assert stored is not None
        assert stored.success_count == 0
        assert stored.failure_count == 1
        assert "secondary" in probe_client.target_names
    finally:
        await client.aclose()


async def test_worker_deletes_proxy_from_blocked_network_without_probe() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    probe_client = StubClient()
    worker, repo = make_worker(
        client,
        probe_client,
        blocked_proxy_networks=("104.24.0.0/14",),
    )
    record = make_record("104.26.15.61:80")
    try:
        await repo.upsert_candidate(record)

        run = await worker.run_once("example.com", now=100.0)

        assert run.deleted == 1
        assert await repo.get_record("example.com", record.endpoint.canonical) is None
        assert probe_client.target_names == ["example"]
    finally:
        await client.aclose()


async def test_cancelled_probe_is_released() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    worker, repo = make_worker(client, StubClient(proxy_error=asyncio.CancelledError()))
    record = make_record()
    try:
        await repo.upsert_candidate(record)

        run = await worker.run_once("example.com", now=100.0)

        assert run.released == 1
        assert await repo.is_due("example.com", record.endpoint.canonical)
    finally:
        await client.aclose()


async def test_worker_deletes_proxy_after_failure_limit() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    worker, repo = make_worker(
        client,
        StubClient(proxy_error=httpx.ProxyError("dead proxy")),
    )
    record = make_record().model_copy(
        update={
            "consecutive_failures": 4,
            "failure_count": 4,
        }
    )
    try:
        await repo.upsert_candidate(record)

        run = await worker.run_once("example.com", now=100.0)

        assert run.deleted == 1
        assert await repo.get_record("example.com", record.endpoint.canonical) is None
    finally:
        await client.aclose()


async def test_run_stops_without_work_when_event_is_set() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    worker, _ = make_worker(client, StubClient())
    stop_event = asyncio.Event()
    stop_event.set()
    try:
        await asyncio.wait_for(worker.run(stop_event), timeout=0.1)
    finally:
        await client.aclose()
