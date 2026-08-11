"""Pure aggregation functions for dashboard data."""

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from ip_proxy_pool.dashboard.models import (
    DashboardAggregate,
    LatencySummary,
    ScoreBuckets,
    SourceQuality,
)
from ip_proxy_pool.models import ProxyRecord, ProxyState
from ip_proxy_pool.storage.repository import PoolStats


@dataclass(slots=True)
class _SourceAccumulator:
    total: int = 0
    tested: int = 0
    available: int = 0
    scores: list[int] = field(default_factory=list)


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _score_buckets(records: list[ProxyRecord]) -> ScoreBuckets:
    counts = {"low": 0, "watch": 0, "usable": 0, "high": 0}
    for record in records:
        if record.score < 60:
            counts["low"] += 1
        elif record.score < 80:
            counts["watch"] += 1
        elif record.score < 90:
            counts["usable"] += 1
        else:
            counts["high"] += 1
    return ScoreBuckets.model_validate(counts)


def _latency(records: list[ProxyRecord]) -> LatencySummary:
    values = [record.latency_ewma_ms for record in records if record.latency_ewma_ms is not None]
    return LatencySummary(
        samples=len(values),
        average_ms=None if not values else sum(values) / len(values),
        p50_ms=_nearest_rank(values, 0.5),
        p95_ms=_nearest_rank(values, 0.95),
    )


def _sources(records: list[ProxyRecord]) -> tuple[SourceQuality, ...]:
    accumulators: dict[str, _SourceAccumulator] = {}
    for record in records:
        for name in record.source_names:
            item = accumulators.setdefault(name, _SourceAccumulator())
            item.total += 1
            item.scores.append(record.score)
            if record.state is not ProxyState.CANDIDATE:
                item.tested += 1
            if record.state is ProxyState.AVAILABLE:
                item.available += 1

    values = [
        SourceQuality(
            name=name,
            total=item.total,
            tested=item.tested,
            available=item.available,
            availability_rate=_rate(item.available, item.tested),
            average_score=sum(item.scores) / len(item.scores),
        )
        for name, item in accumulators.items()
    ]
    return tuple(sorted(values, key=lambda item: (-item.available, -item.total, item.name)))


def aggregate_records(
    records: Iterable[ProxyRecord],
    *,
    stats: PoolStats,
    observed_at: datetime,
    scanned: int,
    partial: bool,
) -> DashboardAggregate:
    values = list(records)
    tested = stats.available + stats.degraded + stats.quarantined
    return DashboardAggregate(
        observed_at=observed_at,
        total=stats.total,
        candidate=stats.candidate,
        available=stats.available,
        degraded=stats.degraded,
        quarantined=stats.quarantined,
        due=stats.due,
        leased=stats.leased,
        availability_rate=_rate(stats.available, tested),
        available_pool_share=_rate(stats.available, stats.total),
        high_quality=sum(
            record.state is ProxyState.AVAILABLE and record.score >= 90 for record in values
        ),
        score_buckets=_score_buckets(values),
        latency=_latency(values),
        sources=_sources(values),
        scanned=scanned,
        partial=partial,
    )
