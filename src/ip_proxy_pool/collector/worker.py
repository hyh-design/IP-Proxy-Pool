from __future__ import annotations

import asyncio
import time
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from ip_proxy_pool.checker.errors import ProbeCategory
from ip_proxy_pool.checker.probe import ProbeClient, probe_proxy
from ip_proxy_pool.checker.scheduling import next_check_at
from ip_proxy_pool.checker.scoring import apply_probe_result
from ip_proxy_pool.collector.cache import SourceCache
from ip_proxy_pool.collector.downloader import DownloadResult
from ip_proxy_pool.collector.health import PredictionFailureCache, SourceHealth
from ip_proxy_pool.collector.models import SourceDefinition
from ip_proxy_pool.collector.pagination import discover_pages
from ip_proxy_pool.collector.parsers import parse_source
from ip_proxy_pool.config import CollectorSettings
from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, TestTarget
from ip_proxy_pool.observability.metrics import Metrics, NoopMetrics
from ip_proxy_pool.security.network import compile_proxy_networks, validate_proxy_endpoint
from ip_proxy_pool.storage.repository import RedisRepository


class Downloader(Protocol):
    async def fetch(self, url: str) -> DownloadResult: ...


@dataclass(slots=True)
class SourceRun:
    source_name: str
    pages_requested: int = 0
    cache_hits: int = 0
    parsed: int = 0
    invalid: int = 0
    predicted: int = 0
    added: int = 0
    proxy_failures: int = 0
    system_errors: int = 0
    skipped_pool_full: int = 0


class _RateLimiter:
    def __init__(self, requests_per_second: float) -> None:
        self._interval = 1.0 / requests_per_second
        self._next_request = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            current = time.monotonic()
            delay = self._next_request - current
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_request = max(current, self._next_request) + self._interval


class CollectorWorker:
    def __init__(
        self,
        *,
        repository: RedisRepository,
        downloader: Downloader,
        cache: SourceCache,
        health: SourceHealth,
        failure_cache: PredictionFailureCache,
        probe_client: ProbeClient,
        target: TestTarget,
        blocked_proxy_networks: Collection[str] = (),
        settings: CollectorSettings,
        metrics: Metrics | None = None,
    ) -> None:
        self.repository = repository
        self.downloader = downloader
        self.cache = cache
        self.health = health
        self.failure_cache = failure_cache
        self.probe_client = probe_client
        self.target = target
        self.blocked_proxy_networks = compile_proxy_networks(blocked_proxy_networks)
        self.settings = settings
        self.metrics = metrics or NoopMetrics()

    async def collect_source(
        self,
        source: SourceDefinition,
        *,
        domain: str,
        now: datetime | None = None,
    ) -> SourceRun:
        current = now or datetime.now(UTC)
        timestamp = current.timestamp()
        run = SourceRun(source_name=source.name)
        if self.target.domain != domain:
            raise ValueError("collector domain must match prediction target")
        if not await self.health.can_run(source.name, now=timestamp):
            return run

        limiter = _RateLimiter(source.requests_per_second)
        first_url = str(source.urls[0])
        try:
            first_result, first_hit = await self._load_page(first_url, limiter=limiter, run=run)
        except Exception:
            run.system_errors += 1
            self.metrics.source_fetch(source.name, "error")
            await self.health.record_failure(source.name, now=timestamp)
            return run
        if first_hit:
            run.cache_hits += 1

        pages = await self.cache.get_pagination(source.name)
        if pages is None:
            pages = discover_pages(
                source,
                first_result.payload,
                global_max_pages=self.settings.max_pages_per_source,
            )
            await self.cache.set_pagination(source.name, pages, ttl=300)

        payloads: list[bytes | None] = [first_result.payload] + [None for _ in pages[1:]]
        page_semaphore = asyncio.Semaphore(self.settings.concurrency)

        async def fetch_page(index: int, url: str) -> None:
            async with page_semaphore:
                try:
                    result, cache_hit = await self._load_page(url, limiter=limiter, run=run)
                except Exception:
                    run.system_errors += 1
                    return
                if cache_hit:
                    run.cache_hits += 1
                payloads[index] = result.payload

        async with asyncio.TaskGroup() as task_group:
            for index, url in enumerate(pages[1:], start=1):
                task_group.create_task(fetch_page(index, url))

        endpoints: list[ProxyEndpoint] = []
        seen: set[str] = set()
        for payload in payloads:
            if payload is None:
                continue
            parsed = parse_source(payload, source)
            run.parsed += len(parsed)
            for endpoint in parsed:
                if endpoint.canonical in seen:
                    continue
                try:
                    validate_proxy_endpoint(
                        endpoint,
                        blocked_networks=self.blocked_proxy_networks,
                    )
                except ValueError:
                    run.invalid += 1
                    continue
                seen.add(endpoint.canonical)
                endpoints.append(endpoint)
                if len(endpoints) >= self.settings.max_proxies_per_source_round:
                    break
            if len(endpoints) >= self.settings.max_proxies_per_source_round:
                break

        if not endpoints:
            self.metrics.source_fetch(source.name, "empty")
            await self.health.record_failure(source.name, now=timestamp)
            return run
        self.metrics.source_fetch(source.name, "success")
        self.metrics.source_parsed(source.name, len(endpoints))
        await self.health.record_success(source.name)

        precheck_semaphore = asyncio.Semaphore(self.settings.concurrency)

        async def ingest(endpoint: ProxyEndpoint) -> None:
            async with precheck_semaphore:
                await self._ingest_endpoint(
                    endpoint,
                    source_name=source.name,
                    domain=domain,
                    now=current,
                    run=run,
                )

        async with asyncio.TaskGroup() as task_group:
            for endpoint in endpoints:
                task_group.create_task(ingest(endpoint))
        return run

    async def _load_page(
        self,
        url: str,
        *,
        limiter: _RateLimiter,
        run: SourceRun,
    ) -> tuple[DownloadResult, bool]:
        cached = await self.cache.get_page(url)
        if cached is not None:
            return cached, True
        await limiter.wait()
        run.pages_requested += 1
        result = await self.downloader.fetch(url)
        await self.cache.set_page(url, result, ttl=300)
        return result, False

    async def _ingest_endpoint(
        self,
        endpoint: ProxyEndpoint,
        *,
        source_name: str,
        domain: str,
        now: datetime,
        run: SourceRun,
    ) -> None:
        canonical = endpoint.canonical
        existing = await self.repository.get_record(domain, canonical)
        if (
            existing is None
            and await self.repository.record_count(domain) >= self.settings.max_pool_size_per_domain
        ):
            run.skipped_pool_full += 1
            return
        candidate = ProxyRecord(
            endpoint=endpoint,
            domain=domain,
            source_names={source_name},
            first_seen_at=existing.first_seen_at if existing is not None else now,
            last_seen_at=now,
            next_check_at=now,
        )
        await self.repository.upsert_candidate(candidate)
        current = await self.repository.get_record(domain, canonical)
        if current is None:
            return
        should_predict = existing is None or existing.next_check_at <= now
        if not should_predict or await self.failure_cache.contains(
            domain, canonical, now=now.timestamp()
        ):
            return

        run.predicted += 1
        result = await probe_proxy(endpoint, self.target, client=self.probe_client)
        self.metrics.record_probe(result.category)
        if result.category is ProbeCategory.SUCCESS:
            verified = apply_probe_result(current, result, now=now)
            verified = verified.model_copy(
                update={"next_check_at": next_check_at(verified, now=now)}
            )
            await self.repository.save_record(verified)
            await self.failure_cache.clear(domain, canonical)
            run.added += 1
        elif result.category is ProbeCategory.PROXY_ERROR:
            await self.failure_cache.mark(domain, canonical, now=now.timestamp())
            run.proxy_failures += 1
        elif result.category is ProbeCategory.SYSTEM_ERROR:
            run.system_errors += 1

    async def collect_round(
        self,
        sources: Iterable[SourceDefinition],
        *,
        domain: str,
        now: datetime | None = None,
    ) -> list[SourceRun]:
        source_list = list(sources)
        semaphore = asyncio.Semaphore(self.settings.concurrency)

        async def collect(source: SourceDefinition) -> SourceRun:
            async with semaphore:
                return await self.collect_source(source, domain=domain, now=now)

        results = list(await asyncio.gather(*(collect(source) for source in source_list)))
        await self.repository.enforce_capacity(
            domain,
            self.settings.max_pool_size_per_domain,
        )
        return results
