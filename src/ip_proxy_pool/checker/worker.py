from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from ip_proxy_pool.checker.circuit_breaker import RedisCircuitBreaker
from ip_proxy_pool.checker.errors import ProbeCategory
from ip_proxy_pool.checker.probe import (
    ProbeClient,
    probe_proxy_targets,
    probe_target_baseline,
)
from ip_proxy_pool.checker.scheduling import next_check_at
from ip_proxy_pool.checker.scoring import apply_probe_result
from ip_proxy_pool.config import CheckerSettings
from ip_proxy_pool.models import TestTarget
from ip_proxy_pool.observability.metrics import Metrics, NoopMetrics
from ip_proxy_pool.security.network import compile_proxy_networks, validate_proxy_endpoint
from ip_proxy_pool.storage.repository import Lease, RedisRepository


@dataclass(slots=True)
class WorkerRun:
    claimed: int = 0
    completed: int = 0
    released: int = 0
    lost_leases: int = 0
    deleted: int = 0


class CheckerWorker:
    def __init__(
        self,
        *,
        repository: RedisRepository,
        breaker: RedisCircuitBreaker,
        probe_client: ProbeClient,
        targets: Mapping[str, TestTarget | Mapping[str, object]],
        validation_targets: Mapping[str, Sequence[TestTarget | Mapping[str, object]]] | None = None,
        blocked_proxy_networks: Collection[str] = (),
        settings: CheckerSettings,
        worker_id: str,
        metrics: Metrics | None = None,
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id must be non-empty")
        self.repository = repository
        self.breaker = breaker
        self.probe_client = probe_client
        self.settings = settings
        self.worker_id = worker_id
        self.metrics = metrics or NoopMetrics()
        self.blocked_proxy_networks = compile_proxy_networks(blocked_proxy_networks)
        self.targets: dict[str, TestTarget] = {}
        self.validation_targets: dict[str, tuple[TestTarget, ...]] = {}
        for domain, raw_target in targets.items():
            target = (
                raw_target
                if isinstance(raw_target, TestTarget)
                else TestTarget.model_validate(raw_target)
            )
            if target.domain != domain:
                raise ValueError("target domain must match mapping key")
            self.targets[domain] = target
        for domain, raw_targets in (validation_targets or {}).items():
            if domain not in self.targets:
                raise ValueError("validation target domain has no primary target")
            parsed_targets: list[TestTarget] = []
            for raw_target in raw_targets:
                validation_target = (
                    raw_target
                    if isinstance(raw_target, TestTarget)
                    else TestTarget.model_validate(raw_target)
                )
                if validation_target.domain != domain:
                    raise ValueError("validation target domain must match primary target")
                parsed_targets.append(validation_target)
            self.validation_targets[domain] = tuple(parsed_targets)

    async def run_once(
        self,
        domain: str,
        *,
        now: float | None = None,
    ) -> WorkerRun:
        target = self.targets.get(domain)
        if target is None:
            raise ValueError(f"no checker target configured for {domain}")
        current_time = time.time() if now is None else now
        current_datetime = datetime.fromtimestamp(current_time, UTC)
        run = WorkerRun()

        await self.repository.reclaim_expired(domain, current_time)
        all_targets = (target, *self.validation_targets.get(domain, ()))
        for validation_target in all_targets:
            decision = await self.breaker.before_probe(
                validation_target.name, owner=self.worker_id, now=current_time
            )
            if not decision.allowed:
                return run

            baseline = await probe_target_baseline(validation_target, client=self.probe_client)
            self.metrics.record_probe(baseline.category)
            if baseline.category is ProbeCategory.SUCCESS:
                await self.breaker.record_success(validation_target.name)
            else:
                if baseline.category is not ProbeCategory.CANCELLED:
                    await self.breaker.record_failure(validation_target.name, now=current_time)
                return run

        leases = await self.repository.claim_due(
            domain,
            self.worker_id,
            self.settings.batch_size,
            self.settings.lease_seconds,
            current_time,
        )
        run.claimed = len(leases)
        semaphore = asyncio.Semaphore(self.settings.concurrency)

        async def process(lease: Lease) -> None:
            try:
                async with semaphore:
                    await self._process_lease(
                        lease,
                        targets=all_targets,
                        now=current_datetime,
                        now_timestamp=current_time,
                        run=run,
                    )
            except asyncio.CancelledError:
                released = await asyncio.shield(self.repository.release(lease, current_time + 5))
                if released:
                    run.released += 1
                else:
                    run.lost_leases += 1

        async with asyncio.TaskGroup() as task_group:
            for lease in leases:
                task_group.create_task(process(lease))
        return run

    async def _process_lease(
        self,
        lease: Lease,
        *,
        targets: tuple[TestTarget, ...],
        now: datetime,
        now_timestamp: float,
        run: WorkerRun,
    ) -> None:
        record = await self.repository.get_record(lease.domain, lease.endpoint)
        if record is None:
            await self._release(lease, now_timestamp, run)
            return

        try:
            validate_proxy_endpoint(
                record.endpoint,
                blocked_networks=self.blocked_proxy_networks,
            )
        except ValueError:
            if await self.repository.delete_leased(lease):
                run.deleted += 1
            else:
                run.lost_leases += 1
            return

        result = await probe_proxy_targets(
            record.endpoint,
            targets,
            client=self.probe_client,
        )
        self.metrics.record_probe(result.category)
        if result.category in {ProbeCategory.SYSTEM_ERROR, ProbeCategory.CANCELLED}:
            await self._release(lease, now_timestamp, run)
            return

        updated = apply_probe_result(record, result, now=now)
        if updated.consecutive_failures >= self.settings.drop_after_failures:
            if await self.repository.delete_leased(lease):
                run.deleted += 1
            else:
                run.lost_leases += 1
            return
        updated = updated.model_copy(update={"next_check_at": next_check_at(updated, now=now)})
        if await self.repository.complete(lease, updated):
            run.completed += 1
        else:
            run.lost_leases += 1

    async def _release(
        self,
        lease: Lease,
        now_timestamp: float,
        run: WorkerRun,
    ) -> None:
        due_at = now_timestamp + random.randint(5, 15)
        if await self.repository.release(lease, due_at):
            run.released += 1
        else:
            run.lost_leases += 1

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            for domain in self.targets:
                if stop_event.is_set():
                    return
                await self.run_once(domain)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1.0)
            except TimeoutError:
                continue
