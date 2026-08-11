from datetime import UTC, datetime

import httpx
import pytest
from redis.asyncio import Redis

from ip_proxy_pool.collector.cache import SourceCache
from ip_proxy_pool.collector.downloader import DownloadResult
from ip_proxy_pool.collector.health import PredictionFailureCache, SourceHealth
from ip_proxy_pool.collector.models import RegexSource
from ip_proxy_pool.collector.worker import CollectorWorker
from ip_proxy_pool.config import CollectorSettings
from ip_proxy_pool.models import ProxyEndpoint, ProxyState
from ip_proxy_pool.models import TestTarget as Target
from ip_proxy_pool.storage.repository import RedisRepository


class Downloader:
    async def fetch(self, url: str) -> DownloadResult:
        del url
        return DownloadResult(
            payload=b"1.1.1.1:80",
            content_type="text/plain",
            fetched_at=datetime(2026, 8, 10, tzinfo=UTC),
        )


class ProbeClient:
    async def request(self, endpoint: ProxyEndpoint | None, target: Target) -> httpx.Response:
        del endpoint, target
        return httpx.Response(200, json={"origin": "1.1.1.1"})


@pytest.mark.docker
async def test_collector_ingests_verified_record(redis_client: Redis) -> None:
    prefix = "ippool:test"
    repository = RedisRepository(redis_client, prefix=prefix)
    worker = CollectorWorker(
        repository=repository,
        downloader=Downloader(),
        cache=SourceCache(redis_client, prefix=prefix),
        health=SourceHealth(redis_client, prefix=prefix),
        failure_cache=PredictionFailureCache(redis_client, prefix=prefix, ttl=3600),
        probe_client=ProbeClient(),
        target=Target.model_validate(
            {
                "name": "example",
                "url": "https://example.com/ip",
                "domain": "example.com",
                "json_keys": ("origin",),
            }
        ),
        settings=CollectorSettings(),
    )
    source = RegexSource.model_validate(
        {
            "name": "integration-source",
            "region": "foreign",
            "urls": ["https://source.example/list"],
            "pattern": r"\d+\.\d+\.\d+\.\d+:\d+",
        }
    )

    run = await worker.collect_source(
        source, domain="example.com", now=datetime(2026, 8, 10, tzinfo=UTC)
    )
    stored = await repository.get_record("example.com", "1.1.1.1:80")

    assert run.added == 1
    assert stored is not None
    assert stored.state is ProxyState.CANDIDATE
    assert stored.score == 75
    assert stored.consecutive_successes == 1
