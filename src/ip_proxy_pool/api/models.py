from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from ip_proxy_pool.models import ProxyRecord, ProxyState
from ip_proxy_pool.storage.repository import PoolStats


class StatusResponse(BaseModel):
    status: str


class DomainsResponse(BaseModel):
    domains: list[str]


class ProxyResponse(BaseModel):
    endpoint: str
    domain: str
    score: int
    state: ProxyState
    source_names: list[str]
    latency_ewma_ms: float | None
    last_checked_at: datetime | None
    next_check_at: datetime

    @classmethod
    def from_record(cls, record: ProxyRecord) -> "ProxyResponse":
        return cls(
            endpoint=record.endpoint.canonical,
            domain=record.domain,
            score=record.score,
            state=record.state,
            source_names=sorted(record.source_names),
            latency_ewma_ms=record.latency_ewma_ms,
            last_checked_at=record.last_checked_at,
            next_check_at=record.next_check_at,
        )


class ProxyPageResponse(BaseModel):
    items: list[ProxyResponse]
    next_cursor: str | None = None


class RandomProxyResponse(BaseModel):
    items: list[ProxyResponse]


class FeedbackOutcome(StrEnum):
    SUCCESS = "success"
    PROXY_ERROR = "proxy_error"


class ProxyFeedbackRequest(BaseModel):
    domain: str = Field(min_length=1, max_length=253)
    endpoint: str = Field(min_length=3, max_length=64)
    outcome: FeedbackOutcome
    latency_ms: float | None = Field(None, ge=0, le=60_000)
    status_code: int | None = Field(None, ge=100, le=599)
    error_type: str | None = Field(None, min_length=1, max_length=64)


class StatsResponse(PoolStats):
    pass


class AdminProbeRequest(BaseModel):
    proxy: str
    url: str
    timeout_seconds: float = Field(5.0, ge=1, le=30)


class AdminProbeResponse(BaseModel):
    category: str
    latency_ms: float | None = None
    status_code: int | None = None
    snippet: str | None = Field(None, max_length=2048)
