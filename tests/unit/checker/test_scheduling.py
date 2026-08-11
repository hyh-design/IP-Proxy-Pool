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


def record(now: datetime, state: ProxyState, score: int) -> ProxyRecord:
    return ProxyRecord(
        endpoint=ProxyEndpoint.parse("1.1.1.1:80"),
        domain="example.com",
        score=score,
        state=state,
        first_seen_at=now,
        last_seen_at=now,
        next_check_at=now,
    )


@pytest.mark.parametrize(
    ("state", "score", "expected"),
    [
        (ProxyState.CANDIDATE, 50, (30, 60)),
        (ProxyState.QUARANTINED, 0, (21600, 86400)),
        (ProxyState.AVAILABLE, 90, (300, 600)),
        (ProxyState.AVAILABLE, 89, (120, 300)),
        (ProxyState.AVAILABLE, 70, (120, 300)),
        (ProxyState.DEGRADED, 69, (30, 90)),
    ],
)
def test_check_intervals(
    now: datetime,
    state: ProxyState,
    score: int,
    expected: tuple[int, int],
) -> None:
    assert check_interval_seconds(record(now, state, score)) == expected


def test_next_check_uses_injected_jitter(now: datetime) -> None:
    checked_ranges: list[tuple[int, int]] = []

    def upper_bound(lower: int, upper: int) -> int:
        checked_ranges.append((lower, upper))
        return upper

    result = next_check_at(record(now, ProxyState.AVAILABLE, 95), now=now, jitter=upper_bound)

    assert checked_ranges == [(300, 600)]
    assert result == now + timedelta(seconds=600)
