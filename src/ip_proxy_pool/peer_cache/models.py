"""Strict wire and cache models for formal-only peer exchange."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord
from ip_proxy_pool.security.network import validate_proxy_endpoint


class PeerExportItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: str
    domain: str = Field(min_length=1, max_length=253)
    score: int = Field(ge=0, le=100)
    latency_ewma_ms: float = Field(ge=0, allow_inf_nan=False)
    last_checked_at: datetime
    consecutive_successes: int = Field(ge=0)
    source_names: tuple[str, ...]

    @field_validator("endpoint")
    @classmethod
    def global_endpoint(cls, value: str) -> str:
        endpoint = validate_proxy_endpoint(ProxyEndpoint.parse(value))
        if endpoint.canonical != value:
            raise ValueError("endpoint must be canonical")
        return value

    @field_validator("last_checked_at")
    @classmethod
    def utc_checked_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UTC timestamp required")
        return value.astimezone(UTC)

    @classmethod
    def from_record(cls, record: ProxyRecord) -> "PeerExportItem":
        if record.latency_ewma_ms is None or record.last_checked_at is None:
            raise ValueError("record lacks peer export evidence")
        return cls(
            endpoint=record.endpoint.canonical,
            domain=record.domain,
            score=record.score,
            latency_ewma_ms=record.latency_ewma_ms,
            last_checked_at=record.last_checked_at,
            consecutive_successes=record.consecutive_successes,
            source_names=tuple(sorted(record.source_names)),
        )


class PeerExportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    origin_node: str = Field(min_length=1, max_length=64)
    scope: Literal["formal"] = "formal"
    generated_at: datetime
    items: tuple[PeerExportItem, ...] = Field(max_length=20)

    @field_validator("generated_at")
    @classmethod
    def utc_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UTC timestamp required")
        return value.astimezone(UTC)


class PeerCacheRecord(PeerExportItem):
    peer_name: str = Field(min_length=1, max_length=64)
    origin_node: str = Field(min_length=1, max_length=64)
    synced_at: datetime
    expires_at: datetime

    @field_validator("synced_at", "expires_at")
    @classmethod
    def utc_cache_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("UTC timestamp required")
        return value.astimezone(UTC)
