from datetime import UTC, datetime, timedelta

import pytest

from ip_proxy_pool.checker.scheduling import (
    check_interval_seconds,
    next_check_at,
)
from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 10, 12, tzinfo=UTC)


def record(
    now: datetime,
    state: ProxyState,
    score: int,
    *,
    consecutive_failures: int = 0,
) -> ProxyRecord:
    return ProxyRecord(
        endpoint=ProxyEndpoint.parse("1.1.1.1:80"),
        domain="example.com",
        score=score,
        state=state,
        consecutive_failures=consecutive_failures,
        first_seen_at=now,
        last_seen_at=now,
        next_check_at=now,
    )


@pytest.mark.parametrize(
    ("state", "score", "expected"),
    [
        (ProxyState.CANDIDATE, 50, (30, 60)),
        (ProxyState.QUARANTINED, 0, (21600, 86400)),
        (ProxyState.AVAILABLE, 90, (180, 300)),
        (ProxyState.AVAILABLE, 89, (900, 1800)),
        (ProxyState.AVAILABLE, 70, (900, 1800)),
        (ProxyState.DEGRADED, 69, (1800, 3600)),
    ],
)
def test_check_intervals(
    now: datetime,
    state: ProxyState,
    score: int,
    expected: tuple[int, int],
) -> None:
    assert check_interval_seconds(record(now, state, score)) == expected


@pytest.mark.parametrize(
    ("consecutive_failures", "expected"),
    [
        (1, (300, 600)),
        (2, (900, 1800)),
        (3, (3600, 7200)),
    ],
)
def test_failures_back_off_before_rechecking(
    now: datetime,
    consecutive_failures: int,
    expected: tuple[int, int],
) -> None:
    failed = record(
        now,
        ProxyState.DEGRADED,
        85,
        consecutive_failures=consecutive_failures,
    )

    assert check_interval_seconds(failed) == expected


def test_quarantined_interval_overrides_failure_backoff(now: datetime) -> None:
    quarantined = record(
        now,
        ProxyState.QUARANTINED,
        0,
        consecutive_failures=5,
    )

    assert check_interval_seconds(quarantined) == (21600, 86400)


def test_next_check_uses_injected_jitter(now: datetime) -> None:
    checked_ranges: list[tuple[int, int]] = []

    def upper_bound(lower: int, upper: int) -> int:
        checked_ranges.append((lower, upper))
        return upper

    result = next_check_at(record(now, ProxyState.AVAILABLE, 95), now=now, jitter=upper_bound)

    assert checked_ranges == [(180, 300)]
    assert result == now + timedelta(seconds=300)
