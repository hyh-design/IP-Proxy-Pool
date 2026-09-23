from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from ip_proxy_pool.peer_cache.models import PeerCacheRecord
from ip_proxy_pool.peer_cache.policy import SelectionPolicy, accepts, effective_peer_policy


def test_effective_policy_never_relaxes_request_or_hard_boundary() -> None:
    request = SelectionPolicy("portal.daqihui.com", 95, 1000, 60, 5)
    service = SelectionPolicy("portal.daqihui.com", 90, 5000, 600, 2)
    cache = SelectionPolicy("portal.daqihui.com", 90, 2000, 600, 2)

    assert effective_peer_policy(request, service, cache) == request
    wide = SelectionPolicy("portal.daqihui.com", 0, 60000, 86400, 1)
    assert effective_peer_policy(wide, service, cache) == cache


def test_peer_record_rejects_invalid_protocol_values() -> None:
    now = datetime.now(UTC)
    base = dict(
        peer_name="system-two",
        origin_node="system-two",
        domain="portal.daqihui.com",
        endpoint="1.1.1.1:80",
        score=95,
        latency_ewma_ms=100,
        last_checked_at=now,
        consecutive_successes=3,
        source_names=["source"],
        synced_at=now,
        expires_at=now + timedelta(seconds=180),
    )
    for change in (
        {"latency_ewma_ms": float("nan")},
        {"latency_ewma_ms": float("inf")},
        {"last_checked_at": None},
        {"endpoint": "127.0.0.1:80"},
    ):
        with pytest.raises(ValidationError):
            PeerCacheRecord.model_validate({**base, **change})


def test_policy_rejects_old_or_future_check() -> None:
    now = datetime.now(UTC)
    base = dict(
        peer_name="system-two",
        origin_node="system-two",
        domain="portal.daqihui.com",
        endpoint="1.1.1.1:80",
        score=95,
        latency_ewma_ms=100,
        consecutive_successes=3,
        source_names=[],
        synced_at=now,
        expires_at=now + timedelta(seconds=180),
    )
    policy = SelectionPolicy("portal.daqihui.com", 90, 2000, 600, 2)
    assert not accepts(
        PeerCacheRecord.model_validate({**base, "last_checked_at": now - timedelta(seconds=600)}),
        policy,
        now,
    )
    assert not accepts(
        PeerCacheRecord.model_validate({**base, "last_checked_at": now + timedelta(seconds=6)}),
        policy,
        now,
    )
