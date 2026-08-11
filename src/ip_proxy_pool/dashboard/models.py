"""Typed dashboard analytics and history policies."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class HistoryRange(StrEnum):
    H1 = "1h"
    H6 = "6h"
    H12 = "12h"
    H24 = "24h"
    D7 = "7d"
    D30 = "30d"


class HistoryResolution(StrEnum):
    FIVE_MINUTES = "5m"
    HOUR = "1h"


@dataclass(frozen=True, slots=True)
class RangePolicy:
    hours: int
    resolution: HistoryResolution
    max_points: int


RANGE_POLICIES = {
    HistoryRange.H1: RangePolicy(1, HistoryResolution.FIVE_MINUTES, 12),
    HistoryRange.H6: RangePolicy(6, HistoryResolution.FIVE_MINUTES, 72),
    HistoryRange.H12: RangePolicy(12, HistoryResolution.FIVE_MINUTES, 144),
    HistoryRange.H24: RangePolicy(24, HistoryResolution.FIVE_MINUTES, 288),
    HistoryRange.D7: RangePolicy(168, HistoryResolution.HOUR, 168),
    HistoryRange.D30: RangePolicy(720, HistoryResolution.HOUR, 720),
}


def range_policy(value: HistoryRange) -> RangePolicy:
    return RANGE_POLICIES[value]


class ScoreBuckets(BaseModel):
    model_config = ConfigDict(frozen=True)

    low: int = Field(0, ge=0)
    watch: int = Field(0, ge=0)
    usable: int = Field(0, ge=0)
    high: int = Field(0, ge=0)


class LatencySummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    samples: int = Field(0, ge=0)
    average_ms: float | None = Field(None, ge=0)
    p50_ms: float | None = Field(None, ge=0)
    p95_ms: float | None = Field(None, ge=0)


class SourceQuality(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    total: int = Field(ge=0)
    tested: int = Field(ge=0)
    available: int = Field(ge=0)
    availability_rate: float | None = Field(None, ge=0, le=1)
    average_score: float | None = Field(None, ge=0, le=100)


class DashboardAggregate(BaseModel):
    model_config = ConfigDict(frozen=True)

    observed_at: datetime
    total: int = Field(ge=0)
    candidate: int = Field(ge=0)
    available: int = Field(ge=0)
    degraded: int = Field(ge=0)
    quarantined: int = Field(ge=0)
    due: int = Field(ge=0)
    leased: int = Field(ge=0)
    availability_rate: float | None = Field(None, ge=0, le=1)
    available_pool_share: float | None = Field(None, ge=0, le=1)
    high_quality: int = Field(ge=0)
    score_buckets: ScoreBuckets
    latency: LatencySummary
    sources: tuple[SourceQuality, ...] = ()
    scanned: int = Field(ge=0)
    partial: bool


class HistoryPoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    observed_at: datetime
    total: int = Field(ge=0)
    candidate: int = Field(ge=0)
    available: int = Field(ge=0)
    degraded: int = Field(ge=0)
    quarantined: int = Field(ge=0)
    due: int = Field(ge=0)
    leased: int = Field(ge=0)
    availability_rate: float | None = Field(None, ge=0, le=1)
    available_pool_share: float | None = Field(None, ge=0, le=1)
    high_quality: int = Field(ge=0)
    score_buckets: ScoreBuckets
    latency: LatencySummary

    @classmethod
    def from_aggregate(cls, value: DashboardAggregate) -> "HistoryPoint":
        return cls.model_validate(value.model_dump(exclude={"sources", "scanned", "partial"}))


class HistorySeries(BaseModel):
    model_config = ConfigDict(frozen=True)

    range: HistoryRange
    resolution: HistoryResolution
    points: tuple[HistoryPoint, ...]
