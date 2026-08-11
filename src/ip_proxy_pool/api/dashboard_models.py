"""Response models for the read-only dashboard API."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from ip_proxy_pool.dashboard.heartbeat import RoleHealth
from ip_proxy_pool.dashboard.models import LatencySummary, ScoreBuckets, SourceQuality


class DashboardSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    domain: str | None = None
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
    latest_snapshot_at: datetime | None = None
    freshness_seconds: float | None = Field(None, ge=0)
    api_status: str
    redis_status: str
    collector: RoleHealth
    checker: RoleHealth
    refresh_seconds: int = Field(30, ge=10, le=300)
    scanned: int = Field(ge=0)
    partial: bool


class QualitySummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    domain: str
    observed_at: datetime
    candidate: int = Field(ge=0)
    available: int = Field(ge=0)
    degraded: int = Field(ge=0)
    quarantined: int = Field(ge=0)
    score_buckets: ScoreBuckets
    latency: LatencySummary
    scanned: int = Field(ge=0)
    partial: bool


class SourceSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    domain: str
    observed_at: datetime
    items: tuple[SourceQuality, ...] = ()
    scanned: int = Field(ge=0)
    partial: bool
