from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from ip_proxy_pool.api.dependencies import (
    get_reclaim_quota_store,
    get_settings,
    require_quota_client,
)
from ip_proxy_pool.config import Settings
from ip_proxy_pool.reclaim_quota.store import ReclaimQuotaStore
from ip_proxy_pool.security.auth import ApiPrincipal

router = APIRouter(prefix="/v1/reclaim/quota", tags=["reclaim-quota"])


class AcquireRequest(BaseModel):
    schema_version: Literal[1]
    domain: Literal["portal.daqihui.com"]
    attempt_id: UUID

    @field_validator("attempt_id")
    @classmethod
    def require_uuid4(cls, value: UUID) -> UUID:
        if value.version != 4:
            raise ValueError("UUIDv4 required")
        return value


class AcquireResponse(BaseModel):
    granted: bool
    retry_after_ms: int
    remaining: int


@router.post("/acquire", response_model=AcquireResponse)
async def acquire_quota(
    body: AcquireRequest,
    principal: Annotated[ApiPrincipal, Depends(require_quota_client)],
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[ReclaimQuotaStore, Depends(get_reclaim_quota_store)],
) -> AcquireResponse:
    del principal
    if not settings.reclaim_quota.enabled:
        raise HTTPException(status_code=503, detail="quota service unavailable")
    try:
        decision = await store.acquire(body.domain, str(body.attempt_id))
    except Exception as error:
        raise HTTPException(status_code=503, detail="quota service unavailable") from error
    if decision.unavailable:
        raise HTTPException(status_code=503, detail="quota service unavailable")
    return AcquireResponse(
        granted=decision.granted,
        retry_after_ms=decision.retry_after_ms,
        remaining=decision.remaining,
    )
