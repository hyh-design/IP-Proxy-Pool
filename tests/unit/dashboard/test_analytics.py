from datetime import UTC, datetime

from ip_proxy_pool.dashboard.analytics import aggregate_records
from ip_proxy_pool.dashboard.models import HistoryRange, range_policy
from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState
from ip_proxy_pool.storage.repository import PoolStats

NOW = datetime(2026, 8, 11, 8, tzinfo=UTC)


def make_record(
    endpoint: str,
    state: ProxyState,
    score: int,
    latency_ms: float | None,
    sources: set[str],
) -> ProxyRecord:
    return ProxyRecord(
        endpoint=ProxyEndpoint.parse(endpoint),
        domain="example.com",
        score=score,
        state=state,
        source_names=sources,
        first_seen_at=NOW,
        last_seen_at=NOW,
        last_checked_at=NOW if state is not ProxyState.CANDIDATE else None,
        next_check_at=NOW,
        latency_ewma_ms=latency_ms,
    )


def test_history_ranges_choose_fixed_resolution_and_limits() -> None:
    assert range_policy(HistoryRange.H12).resolution.value == "5m"
    assert range_policy(HistoryRange.H12).max_points == 144
    assert range_policy(HistoryRange.D7).resolution.value == "1h"
    assert range_policy(HistoryRange.D30).max_points == 720


def test_aggregate_uses_tested_denominator_and_nearest_rank_latency() -> None:
    records = [
        make_record("1.1.1.1:80", ProxyState.AVAILABLE, 95, 100.0, {"alpha"}),
        make_record("8.8.8.8:80", ProxyState.DEGRADED, 75, 300.0, {"alpha", "beta"}),
        make_record("9.9.9.9:80", ProxyState.CANDIDATE, 50, None, {"beta"}),
    ]

    value = aggregate_records(
        records,
        stats=PoolStats(total=3, available=1, degraded=1, candidate=1),
        observed_at=NOW,
        scanned=3,
        partial=False,
    )

    assert value.availability_rate == 0.5
    assert value.available_pool_share == 1 / 3
    assert value.high_quality == 1
    assert value.score_buckets.model_dump() == {
        "low": 1,
        "watch": 1,
        "usable": 0,
        "high": 1,
    }
    assert value.latency.model_dump() == {
        "samples": 2,
        "average_ms": 200.0,
        "p50_ms": 100.0,
        "p95_ms": 300.0,
    }
    assert [(item.name, item.total, item.available) for item in value.sources] == [
        ("alpha", 2, 1),
        ("beta", 2, 0),
    ]


def test_empty_aggregate_reports_unknown_rates_and_latency() -> None:
    value = aggregate_records(
        [],
        stats=PoolStats(),
        observed_at=NOW,
        scanned=0,
        partial=False,
    )

    assert value.availability_rate is None
    assert value.available_pool_share is None
    assert value.latency.average_ms is None
    assert value.latency.p50_ms is None
    assert value.latency.p95_ms is None
    assert value.sources == ()
