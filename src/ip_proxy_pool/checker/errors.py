from enum import StrEnum

from pydantic import BaseModel, Field


class ProbeCategory(StrEnum):
    SUCCESS = "success"
    PROXY_ERROR = "proxy_error"
    SYSTEM_ERROR = "system_error"
    CANCELLED = "cancelled"


class ProbeResult(BaseModel):
    category: ProbeCategory
    latency_ms: float | None = Field(None, ge=0)
    status_code: int | None = Field(None, ge=100, le=599)
    error_type: str | None = Field(None, max_length=64)
    error_message: str | None = Field(None, max_length=256)
