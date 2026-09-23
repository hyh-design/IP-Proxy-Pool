"""Effective peer bounds, independent of formal query defaults."""

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SelectionPolicy:
    domain: str
    min_score: int
    max_latency_ms: float
    max_checked_age_seconds: int
    min_consecutive_successes: int

    def __post_init__(self) -> None:
        if not self.domain or not 0 <= self.min_score <= 100:
            raise ValueError("invalid domain or score")
        if not math.isfinite(self.max_latency_ms) or self.max_latency_ms < 0:
            raise ValueError("invalid latency bound")
        if self.max_checked_age_seconds <= 0 or self.min_consecutive_successes <= 0:
            raise ValueError("invalid freshness or success bound")


class SelectionEvidence(Protocol):
    @property
    def domain(self) -> str: ...

    @property
    def score(self) -> int: ...

    @property
    def latency_ewma_ms(self) -> float | None: ...

    @property
    def last_checked_at(self) -> datetime | None: ...

    @property
    def consecutive_successes(self) -> int: ...


def effective_peer_policy(
    request: SelectionPolicy, service: SelectionPolicy, cache: SelectionPolicy
) -> SelectionPolicy:
    if request.domain != service.domain or request.domain != cache.domain:
        raise ValueError("peer policy domain mismatch")
    return SelectionPolicy(
        domain=request.domain,
        min_score=max(request.min_score, service.min_score, cache.min_score, 90),
        max_latency_ms=min(
            request.max_latency_ms, service.max_latency_ms, cache.max_latency_ms, 2000
        ),
        max_checked_age_seconds=min(
            request.max_checked_age_seconds,
            service.max_checked_age_seconds,
            cache.max_checked_age_seconds,
            600,
        ),
        min_consecutive_successes=max(
            request.min_consecutive_successes,
            service.min_consecutive_successes,
            cache.min_consecutive_successes,
            2,
        ),
    )


def accepts(record: SelectionEvidence, policy: SelectionPolicy, now: datetime) -> bool:
    if record.domain != policy.domain or record.score < policy.min_score:
        return False
    latency = record.latency_ewma_ms
    checked_at = record.last_checked_at
    if (
        latency is None
        or not math.isfinite(latency)
        or latency < 0
        or latency > policy.max_latency_ms
    ):
        return False
    if checked_at is None or checked_at.tzinfo is None or checked_at.utcoffset() is None:
        return False
    age = (now - checked_at).total_seconds()
    if age < -5 or age >= policy.max_checked_age_seconds:
        return False
    if record.consecutive_successes < policy.min_consecutive_successes:
        return False
    expires_at = getattr(record, "expires_at", None)
    return expires_at is None or now < expires_at
