from __future__ import annotations

import asyncio
import json
import signal
import sys
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import uvicorn
from redis.asyncio import Redis

from ip_proxy_pool.api.app import create_app
from ip_proxy_pool.checker.circuit_breaker import RedisCircuitBreaker
from ip_proxy_pool.checker.probe import HttpxProbeClient
from ip_proxy_pool.checker.worker import CheckerWorker
from ip_proxy_pool.collector.cache import SourceCache
from ip_proxy_pool.collector.downloader import SourceDownloader
from ip_proxy_pool.collector.health import PredictionFailureCache, SourceHealth
from ip_proxy_pool.collector.sources import sources_for_region
from ip_proxy_pool.collector.worker import CollectorWorker
from ip_proxy_pool.config import Settings
from ip_proxy_pool.dashboard.heartbeat import WorkerHeartbeatStore
from ip_proxy_pool.dashboard.history import DashboardHistoryStore
from ip_proxy_pool.dashboard.snapshot import DashboardSnapshotRecorder
from ip_proxy_pool.models import TestTarget
from ip_proxy_pool.storage.repository import RedisRepository


class _NoSignalServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def target_from_settings(settings: Settings) -> TestTarget:
    return settings.target.to_test_target()


def validation_targets_from_settings(settings: Settings) -> tuple[TestTarget, ...]:
    return settings.target.to_validation_targets()


async def inventory_requires_refill(
    repository: RedisRepository,
    settings: Settings,
    *,
    domain: str,
) -> bool:
    """Return whether the consumer-grade hot pool is below its refill threshold."""
    collector = settings.collector
    threshold = collector.low_inventory_threshold
    if threshold == 0:
        return False
    selection = settings.selection
    records = await repository.random_proxies(
        domain=domain,
        min_score=max(collector.low_inventory_min_score, selection.min_score),
        count=threshold,
        max_latency_ms=min(
            collector.low_inventory_max_latency_ms,
            selection.max_latency_ms,
        ),
        max_checked_age_seconds=selection.max_checked_age_seconds,
        min_consecutive_successes=selection.min_consecutive_successes,
    )
    return len(records) < threshold


async def run_dashboard_maintenance(
    stop_event: asyncio.Event,
    *,
    role: str,
    instance_id: str,
    heartbeat: WorkerHeartbeatStore,
    recorder: DashboardSnapshotRecorder | None,
    interval_seconds: int,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Publish worker liveness and, for the checker, periodic snapshots."""
    while not stop_event.is_set():
        current = now()
        await heartbeat.beat(role, instance_id, now=current)
        if recorder is not None:
            await recorder.record_if_due(current)
        if stop_event.is_set():
            break
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


def _start_dashboard_maintenance(
    settings: Settings,
    coordinator: ShutdownCoordinator,
    *,
    role: str,
    instance_id: str,
    heartbeat: WorkerHeartbeatStore,
    recorder: DashboardSnapshotRecorder | None,
) -> asyncio.Task[None] | None:
    if not settings.dashboard.enabled:
        return None
    task = asyncio.create_task(
        run_dashboard_maintenance(
            coordinator.stop_event,
            role=role,
            instance_id=instance_id,
            heartbeat=heartbeat,
            recorder=recorder,
            interval_seconds=settings.dashboard.heartbeat_interval_seconds,
        )
    )
    coordinator.track_task(task)
    return task


async def _cancel_task(task: asyncio.Task[None] | None) -> None:
    if task is None:
        return
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


class ShutdownCoordinator:
    def __init__(self, *, grace_seconds: float = 30) -> None:
        if grace_seconds <= 0:
            raise ValueError("grace_seconds must be positive")
        self.grace_seconds = grace_seconds
        self.stop_event = asyncio.Event()
        self._resources: list[Any] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stopped = False

    def register_resource(self, resource: Any) -> None:
        self._resources.append(resource)

    def track_task(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def install_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for handled_signal in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(
                    handled_signal,
                    lambda: asyncio.create_task(self.stop()),
                )
            except (NotImplementedError, RuntimeError):
                continue

    async def _invoke(self, method_name: str) -> None:
        for resource in self._resources:
            method = getattr(resource, method_name, None)
            if method is not None:
                result = method()
                if isinstance(result, Awaitable):
                    await result

    async def stop(self) -> int:
        if self._stopped:
            return 0
        self._stopped = True
        self.stop_event.set()
        await self._invoke("stop_new_work")
        current = asyncio.current_task()
        active = [task for task in self._tasks if task is not current and not task.done()]
        timed_out = False
        if active:
            done, pending = await asyncio.wait(active, timeout=self.grace_seconds)
            del done
            if pending:
                timed_out = True
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        await self._invoke("release_inflight")
        await self._invoke("close")
        return 1 if timed_out else 0


async def run_api(settings: Settings, coordinator: ShutdownCoordinator) -> int:
    server = _NoSignalServer(
        uvicorn.Config(
            create_app(settings),
            host=settings.api.host,
            port=settings.api.port,
            log_config=None,
        )
    )
    task = asyncio.create_task(server.serve())
    coordinator.track_task(task)
    stop_task = asyncio.create_task(coordinator.stop_event.wait())
    done, _ = await asyncio.wait({task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    if stop_task in done:
        server.should_exit = True
        await task
    else:
        stop_task.cancel()
    return 0 if not task.cancelled() and task.exception() is None else 1


async def run_collector(
    settings: Settings,
    coordinator: ShutdownCoordinator,
    *,
    region: str = "all",
) -> int:
    redis = Redis.from_url(str(settings.redis.url), decode_responses=True)
    repository = RedisRepository(redis, prefix=settings.redis.key_prefix)
    downloader = SourceDownloader(
        max_bytes=settings.collector.max_response_bytes,
        timeout=10,
    )
    target = target_from_settings(settings)
    worker = CollectorWorker(
        repository=repository,
        downloader=downloader,
        cache=SourceCache(redis, prefix=settings.redis.key_prefix),
        health=SourceHealth(redis, prefix=settings.redis.key_prefix),
        failure_cache=PredictionFailureCache(redis, prefix=settings.redis.key_prefix, ttl=14_400),
        probe_client=HttpxProbeClient(),
        target=target,
        blocked_proxy_networks=settings.security.blocked_proxy_networks,
        settings=settings.collector,
    )
    maintenance_task: asyncio.Task[None] | None = None
    if settings.dashboard.enabled:
        heartbeat = WorkerHeartbeatStore(
            redis,
            prefix=settings.redis.key_prefix,
            interval_seconds=settings.dashboard.heartbeat_interval_seconds,
            ttl_seconds=settings.dashboard.heartbeat_ttl_seconds,
        )
        maintenance_task = _start_dashboard_maintenance(
            settings,
            coordinator,
            role="collector",
            instance_id=f"collector-{uuid4().hex}",
            heartbeat=heartbeat,
            recorder=None,
        )
    try:
        force_refresh = False
        while not coordinator.stop_event.is_set():
            await worker.collect_round(
                sources_for_region(region),
                domain=target.domain,
                now=datetime.now(UTC),
                force_refresh=force_refresh,
            )
            force_refresh = False
            elapsed = 0
            while elapsed < settings.collector.collection_interval_seconds:
                timeout = min(
                    settings.collector.inventory_check_interval_seconds,
                    settings.collector.collection_interval_seconds - elapsed,
                )
                try:
                    await asyncio.wait_for(coordinator.stop_event.wait(), timeout=timeout)
                    break
                except TimeoutError:
                    elapsed += timeout
                if elapsed >= settings.collector.collection_interval_seconds:
                    break
                if await inventory_requires_refill(
                    repository,
                    settings,
                    domain=target.domain,
                ):
                    force_refresh = True
                    break
    finally:
        await _cancel_task(maintenance_task)
        await downloader.close()
        await redis.aclose()
    return 0


async def run_checker(settings: Settings, coordinator: ShutdownCoordinator) -> int:
    redis = Redis.from_url(str(settings.redis.url), decode_responses=True)
    repository = RedisRepository(redis, prefix=settings.redis.key_prefix)
    target = target_from_settings(settings)
    validation_targets = validation_targets_from_settings(settings)
    worker = CheckerWorker(
        repository=repository,
        breaker=RedisCircuitBreaker(
            redis,
            prefix=settings.redis.key_prefix,
            failures=settings.checker.breaker_failures,
            cooldown=settings.checker.breaker_cooldown_seconds,
        ),
        probe_client=HttpxProbeClient(),
        targets={target.domain: target},
        validation_targets={target.domain: validation_targets},
        blocked_proxy_networks=settings.security.blocked_proxy_networks,
        settings=settings.checker,
        worker_id="checker",
    )
    maintenance_task: asyncio.Task[None] | None = None
    if settings.dashboard.enabled:
        heartbeat = WorkerHeartbeatStore(
            redis,
            prefix=settings.redis.key_prefix,
            interval_seconds=settings.dashboard.heartbeat_interval_seconds,
            ttl_seconds=settings.dashboard.heartbeat_ttl_seconds,
        )
        history = DashboardHistoryStore(
            redis,
            prefix=settings.redis.key_prefix,
            short_hours=settings.dashboard.short_retention_hours,
            long_hours=settings.dashboard.long_retention_hours,
        )
        recorder = DashboardSnapshotRecorder(
            redis,
            repository=repository,
            history=history,
            prefix=settings.redis.key_prefix,
            settings=settings.dashboard,
        )
        maintenance_task = _start_dashboard_maintenance(
            settings,
            coordinator,
            role="checker",
            instance_id=f"checker-{uuid4().hex}",
            heartbeat=heartbeat,
            recorder=recorder,
        )
    try:
        await worker.run(coordinator.stop_event)
    finally:
        await _cancel_task(maintenance_task)
        await redis.aclose()
    return 0


async def run_all(settings: Settings, coordinator: ShutdownCoordinator) -> int:
    tasks = {
        asyncio.create_task(run_api(settings, coordinator)),
        asyncio.create_task(run_collector(settings, coordinator)),
        asyncio.create_task(run_checker(settings, coordinator)),
    }
    for task in tasks:
        coordinator.track_task(task)
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    failed = any(task.exception() is not None or task.result() != 0 for task in done)
    if failed:
        await coordinator.stop()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return 1 if failed else 0


async def doctor(settings: Settings) -> int:
    checks: dict[str, dict[str, object]] = {}
    try:
        settings.validate_api_startup()
        checks["settings"] = {"ok": True}
    except ValueError as error:
        checks["settings"] = {"ok": False, "error": str(error)[:256]}
    checks["python"] = {"ok": sys.version_info >= (3, 11), "version": sys.version.split()[0]}
    checks["httpx"] = {"ok": True, "version": httpx.__version__}
    sources = sources_for_region("all")
    checks["sources"] = {"ok": bool(sources), "count": len(sources)}
    redis = Redis.from_url(str(settings.redis.url), decode_responses=True)
    try:
        checks["redis"] = {"ok": bool(await redis.ping())}
    except Exception as error:
        checks["redis"] = {"ok": False, "error": type(error).__name__}
    finally:
        await redis.aclose()
    ok = all(bool(item["ok"]) for item in checks.values())
    print(json.dumps({"ok": ok, "checks": checks}, sort_keys=True))
    return 0 if ok else 1
