import asyncio
from datetime import UTC, datetime

import fakeredis.aioredis
import httpx

from ip_proxy_pool.collector.cache import SourceCache
from ip_proxy_pool.collector.downloader import DownloadResult
from ip_proxy_pool.collector.health import PredictionFailureCache, SourceHealth
from ip_proxy_pool.collector.models import RegexSource
from ip_proxy_pool.collector.worker import CollectorWorker
from ip_proxy_pool.config import CollectorSettings
from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState
from ip_proxy_pool.models import TestTarget as Target
from ip_proxy_pool.storage.repository import RedisRepository


class StubDownloader:
    def __init__(
        self,
        outcomes: dict[str, bytes | Exception],
        *,
        delay: float = 0,
    ) -> None:
        self.outcomes = outcomes
        self.delay = delay
        self.requests = 0
        self.active = 0
        self.max_active = 0

    async def fetch(self, url: str) -> DownloadResult:
        self.requests += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            outcome = self.outcomes[url]
            if isinstance(outcome, Exception):
                raise outcome
            return DownloadResult(
                payload=outcome,
                content_type="text/plain",
                fetched_at=datetime(2026, 8, 10, tzinfo=UTC),
            )
        finally:
            self.active -= 1


class StubProbeClient:
    def __init__(self) -> None:
        self.proxy_errors: dict[str, BaseException] = {}

    async def request(self, endpoint: ProxyEndpoint | None, target: Target) -> httpx.Response:
        del target
        if endpoint is not None:
            error = self.proxy_errors.get(endpoint.canonical)
            if error is not None:
                raise error
        return httpx.Response(200, json={"origin": "1.1.1.1"})


def regex_source(*urls: str, max_pages: int = 20) -> RegexSource:
    return RegexSource.model_validate(
        {
            "name": "test-source",
            "region": "foreign",
            "urls": urls,
            "max_pages": max_pages,
            "requests_per_second": 10,
            "pattern": r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}(?!\d)",
        }
    )


def target() -> Target:
    return Target.model_validate(
        {
            "name": "example",
            "url": "https://example.com/ip",
            "domain": "example.com",
            "json_keys": ("origin",),
        }
    )


def make_collector(
    client: fakeredis.aioredis.FakeRedis,
    downloader: StubDownloader,
    probe_client: StubProbeClient,
    *,
    concurrency: int = 2,
    cap: int = 10_000,
    pool_cap: int = 10_000,
    blocked_proxy_networks: tuple[str, ...] = (),
) -> tuple[CollectorWorker, RedisRepository, SourceCache, PredictionFailureCache]:
    repository = RedisRepository(client, prefix="ippool:test")
    cache = SourceCache(client, prefix="ippool:test")
    failure_cache = PredictionFailureCache(client, prefix="ippool:test", ttl=3600)
    collector = CollectorWorker(
        repository=repository,
        downloader=downloader,
        cache=cache,
        health=SourceHealth(client, prefix="ippool:test", jitter=lambda lower, upper: lower),
        failure_cache=failure_cache,
        probe_client=probe_client,
        target=target(),
        blocked_proxy_networks=blocked_proxy_networks,
        settings=CollectorSettings(
            concurrency=concurrency,
            max_pages_per_source=20,
            max_response_bytes=2048,
            max_proxies_per_source_round=cap,
            max_pool_size_per_domain=pool_cap,
        ),
    )
    return collector, repository, cache, failure_cache


async def test_blocked_proxy_network_is_rejected_before_prediction() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    url = "https://source.example/blocked"
    downloader = StubDownloader({url: b"104.26.15.61:80\n1.1.1.1:80"})
    collector, repo, _, _ = make_collector(
        client,
        downloader,
        StubProbeClient(),
        blocked_proxy_networks=("104.24.0.0/14",),
    )
    try:
        run = await collector.collect_source(
            regex_source(url),
            domain="example.com",
            now=datetime(2026, 8, 10, tzinfo=UTC),
        )

        assert run.invalid == 1
        assert run.predicted == 1
        assert await repo.get_record("example.com", "104.26.15.61:80") is None
    finally:
        await client.aclose()


async def test_prediction_success_remains_candidate_until_second_probe() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    url = "https://source.example/one"
    downloader = StubDownloader({url: b"1.1.1.1:80"})
    collector, repo, _, _ = make_collector(client, downloader, StubProbeClient())
    source = regex_source(url)
    try:
        run = await collector.collect_source(
            source, domain="example.com", now=datetime(2026, 8, 10, tzinfo=UTC)
        )
        record = await repo.get_record("example.com", "1.1.1.1:80")

        assert run.added == 1
        assert run.predicted == 1
        assert record is not None
        assert record.state is ProxyState.CANDIDATE
        assert record.consecutive_successes == 1
        assert record.score == 75
    finally:
        await client.aclose()


async def test_cached_first_page_avoids_network_request() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    url = "https://source.example/cached"
    downloader = StubDownloader({})
    collector, _, cache, _ = make_collector(client, downloader, StubProbeClient())
    source = regex_source(url)
    now = datetime(2026, 8, 10, tzinfo=UTC)
    try:
        await cache.set_page(
            url,
            DownloadResult(payload=b"1.1.1.1:80", content_type=None, fetched_at=now),
            ttl=60,
        )

        run = await collector.collect_source(source, domain="example.com", now=now)

        assert run.cache_hits == 1
        assert downloader.requests == 0
        assert run.added == 1
    finally:
        await client.aclose()


async def test_bad_page_does_not_discard_good_pages_and_cap_is_enforced() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    urls = tuple(f"https://source.example/{number}" for number in range(4))
    downloader = StubDownloader(
        {
            urls[0]: b"1.1.1.1:80\n8.8.8.8:80",
            urls[1]: httpx.ReadTimeout("bad page"),
            urls[2]: b"9.9.9.9:80",
            urls[3]: b"4.2.2.2:80",
        },
        delay=0.02,
    )
    collector, _, _, _ = make_collector(client, downloader, StubProbeClient(), concurrency=2, cap=2)
    try:
        run = await collector.collect_source(
            regex_source(*urls),
            domain="example.com",
            now=datetime(2026, 8, 10, tzinfo=UTC),
        )

        assert run.added == 2
        assert run.parsed >= 2
        assert run.system_errors == 1
        assert downloader.max_active <= 2
    finally:
        await client.aclose()


async def test_collector_skips_new_proxy_when_domain_pool_is_full() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    url = "https://source.example/full"
    collector, repo, _, _ = make_collector(
        client,
        StubDownloader({url: b"8.8.8.8:80"}),
        StubProbeClient(),
        pool_cap=1,
    )
    now = datetime(2026, 8, 10, tzinfo=UTC)
    existing = ProxyRecord(
        endpoint=ProxyEndpoint.parse("1.1.1.1:80"),
        domain="example.com",
        first_seen_at=now,
        last_seen_at=now,
        next_check_at=now,
    )
    try:
        await repo.upsert_candidate(existing)

        run = await collector.collect_source(regex_source(url), domain="example.com", now=now)

        assert run.skipped_pool_full == 1
        assert await repo.get_record("example.com", "8.8.8.8:80") is None
    finally:
        await client.aclose()


async def test_collect_round_prunes_domain_back_to_capacity() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    collector, repo, _, _ = make_collector(
        client,
        StubDownloader({}),
        StubProbeClient(),
        pool_cap=1,
    )
    now = datetime(2026, 8, 10, tzinfo=UTC)
    try:
        for address in ("1.1.1.1:80", "8.8.8.8:80"):
            await repo.upsert_candidate(
                ProxyRecord(
                    endpoint=ProxyEndpoint.parse(address),
                    domain="example.com",
                    first_seen_at=now,
                    last_seen_at=now,
                    next_check_at=now,
                )
            )

        await collector.collect_round([], domain="example.com", now=now)

        assert await repo.record_count("example.com") == 1
    finally:
        await client.aclose()


async def test_system_prediction_error_does_not_poison_failure_cache() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    url = "https://source.example/system-error"
    probes = StubProbeClient()
    probes.proxy_errors["1.1.1.1:80"] = RuntimeError("local bug")
    collector, repo, _, failure_cache = make_collector(
        client, StubDownloader({url: b"1.1.1.1:80"}), probes
    )
    now = datetime(2026, 8, 10, tzinfo=UTC)
    try:
        run = await collector.collect_source(regex_source(url), domain="example.com", now=now)

        assert run.system_errors == 1
        assert run.proxy_failures == 0
        assert not await failure_cache.contains("example.com", "1.1.1.1:80", now=now.timestamp())
        record = await repo.get_record("example.com", "1.1.1.1:80")
        assert record is not None and record.state is ProxyState.CANDIDATE
    finally:
        await client.aclose()


async def test_proxy_prediction_failure_is_cached() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    url = "https://source.example/proxy-error"
    probes = StubProbeClient()
    probes.proxy_errors["1.1.1.1:80"] = httpx.ProxyError("refused")
    collector, _, _, failure_cache = make_collector(
        client, StubDownloader({url: b"1.1.1.1:80"}), probes
    )
    now = datetime(2026, 8, 10, tzinfo=UTC)
    try:
        run = await collector.collect_source(regex_source(url), domain="example.com", now=now)

        assert run.proxy_failures == 1
        assert await failure_cache.contains("example.com", "1.1.1.1:80", now=now.timestamp())
    finally:
        await client.aclose()


async def test_source_health_backoff_increases_and_resets() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    health = SourceHealth(client, prefix="ippool:test", jitter=lambda lower, upper: lower)
    try:
        delays = [await health.record_failure("source-a", now=float(index)) for index in range(6)]
        await health.record_success("source-a")

        assert delays == [30, 60, 120, 300, 600, 600]
        assert await health.can_run("source-a", now=1000) is True
    finally:
        await client.aclose()
