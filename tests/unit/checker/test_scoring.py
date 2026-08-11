from datetime import UTC, datetime

import pytest

from ip_proxy_pool.checker.errors import ProbeCategory, ProbeResult
from ip_proxy_pool.checker.scoring import apply_probe_result
from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 10, 12, tzinfo=UTC)


@pytest.fixture
def candidate(now: datetime) -> ProxyRecord:
    return ProxyRecord(
        endpoint=ProxyEndpoint.parse("1.1.1.1:80"),
        domain="example.com",
        source_names={"test"},
        first_seen_at=now,
        last_seen_at=now,
        next_check_at=now,
    )


def test_system_error_does_not_change_failure_state(candidate: ProxyRecord, now: datetime) -> None:
    result = ProbeResult(category=ProbeCategory.SYSTEM_ERROR, error_type="dns")

    updated = apply_probe_result(candidate, result, now=now)

    assert updated == candidate


def test_cancelled_probe_does_not_change_health(candidate: ProxyRecord, now: datetime) -> None:
    result = ProbeResult(category=ProbeCategory.CANCELLED, error_type="cancelled")

    assert apply_probe_result(candidate, result, now=now) == candidate


def test_three_proxy_failures_quarantine(candidate: ProxyRecord, now: datetime) -> None:
    record = candidate
    failure = ProbeResult(
        category=ProbeCategory.PROXY_ERROR,
        error_type="connect",
        error_message="refused",
    )

    for _ in range(3):
        record = apply_probe_result(record, failure, now=now)

    assert record.score == 0
    assert record.state is ProxyState.QUARANTINED
    assert record.failure_count == 3
    assert record.consecutive_failures == 3
    assert record.last_error_type == "connect"


def test_success_recovers_and_updates_latency_ewma(candidate: ProxyRecord, now: datetime) -> None:
    unhealthy = candidate.model_copy(
        update={
            "score": 20,
            "state": ProxyState.DEGRADED,
            "consecutive_failures": 2,
            "latency_ewma_ms": 100.0,
            "last_error_type": "connect",
        }
    )
    result = ProbeResult(
        category=ProbeCategory.SUCCESS,
        latency_ms=200.0,
        status_code=200,
    )

    first = apply_probe_result(unhealthy, result, now=now)
    updated = apply_probe_result(first, result, now=now)

    assert updated.score == 85
    assert updated.state is ProxyState.AVAILABLE
    assert updated.success_count == 2
    assert updated.consecutive_successes == 2
    assert updated.consecutive_failures == 0
    assert updated.latency_ewma_ms == pytest.approx(151.0)
    assert updated.last_checked_at == now
    assert updated.last_error_type is None


def test_first_success_stays_candidate_until_confirmed(
    candidate: ProxyRecord, now: datetime
) -> None:
    result = ProbeResult(category=ProbeCategory.SUCCESS, latency_ms=100, status_code=200)

    updated = apply_probe_result(candidate, result, now=now)

    assert updated.state is ProxyState.CANDIDATE
    assert updated.score == 75
    assert updated.consecutive_successes == 1


def test_score_is_clamped_at_both_boundaries(candidate: ProxyRecord, now: datetime) -> None:
    success = ProbeResult(category=ProbeCategory.SUCCESS, latency_ms=10)
    maximum = apply_probe_result(candidate.model_copy(update={"score": 100}), success, now=now)
    failure = ProbeResult(category=ProbeCategory.PROXY_ERROR)
    minimum = candidate.model_copy(update={"score": 1})
    minimum = apply_probe_result(minimum, failure, now=now)

    assert maximum.score == 100
    assert minimum.score == 0
    assert minimum.state is ProxyState.QUARANTINED
