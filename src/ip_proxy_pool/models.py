"""Core immutable and persisted domain models."""

from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, IPvAnyAddress


class ProxyState(StrEnum):
    CANDIDATE = "candidate"
    AVAILABLE = "available"
    DEGRADED = "degraded"
    QUARANTINED = "quarantined"


class ProxyEndpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    host: IPvAnyAddress
    port: int = Field(ge=1, le=65535)

    @classmethod
    def parse(cls, value: str) -> "ProxyEndpoint":
        raw = value.strip()
        if raw.startswith("["):
            parsed = urlsplit(f"//{raw}")
            if parsed.hostname is None or parsed.port is None:
                raise ValueError("invalid bracketed IPv6 proxy endpoint")
            return cls.model_validate({"host": parsed.hostname, "port": parsed.port})

        host, separator, port = raw.rpartition(":")
        if not separator or not host or ":" in host:
            raise ValueError("proxy endpoint must be IPv4:port or [IPv6]:port")
        return cls.model_validate({"host": host, "port": int(port)})

    @property
    def canonical(self) -> str:
        host = str(self.host)
        return f"[{host}]:{self.port}" if self.host.version == 6 else f"{host}:{self.port}"


class TestTarget(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=64)
    url: AnyHttpUrl
    domain: str = Field(min_length=1, max_length=253)
    expected_statuses: frozenset[int] = frozenset({200})
    expected_text: str | None = None
    json_keys: tuple[str, ...] = ()
    timeout_seconds: float = Field(5.0, ge=0.5, le=30)


class ProxyRecord(BaseModel):
    endpoint: ProxyEndpoint
    domain: str = Field(min_length=1, max_length=253)
    score: int = Field(50, ge=0, le=100)
    state: ProxyState = ProxyState.CANDIDATE
    source_names: set[str] = Field(default_factory=set)
    first_seen_at: datetime
    last_seen_at: datetime
    last_checked_at: datetime | None = None
    next_check_at: datetime
    consecutive_successes: int = Field(0, ge=0)
    consecutive_failures: int = Field(0, ge=0)
    success_count: int = Field(0, ge=0)
    failure_count: int = Field(0, ge=0)
    latency_ewma_ms: float | None = Field(None, ge=0)
    last_status_code: int | None = Field(None, ge=100, le=599)
    last_error_type: str | None = Field(None, max_length=64)
    last_error_message: str | None = Field(None, max_length=256)
