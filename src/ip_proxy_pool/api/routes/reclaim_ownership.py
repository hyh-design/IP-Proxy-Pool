"""Versioned, member-scoped cross-system reclaim ownership API."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import UUID4, BaseModel, ConfigDict, Field

from ip_proxy_pool.api.dependencies import (
    enforce_ownership_limit,
    get_ownership_store,
    get_settings,
)
from ip_proxy_pool.config import Settings
from ip_proxy_pool.reclaim_ownership.models import CaseSnapshot, CheckJob, LeadCycle
from ip_proxy_pool.reclaim_ownership.store import OwnershipStore, SuccessConflict
from ip_proxy_pool.security.auth import ApiPrincipal

router = APIRouter(prefix="/v1/reclaim/ownership", tags=["reclaim-ownership"])


class VersionedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]


class SuccessRequest(VersionedRequest):
    event_id: UUID4
    cycle: LeadCycle


class CaseRequest(VersionedRequest):
    case_id: UUID4
    cycle: LeadCycle


class CheckRequest(VersionedRequest):
    case_id: UUID4
    round_no: Literal[1, 2]
    result: Literal["found", "absent", "error"]
    result_id: UUID4
    lease_token: str = Field(min_length=1, max_length=128)


class HeartbeatRequest(VersionedRequest):
    success_backlog: int = Field(0, ge=0, le=1_000_000)


class CaseResponse(CaseSnapshot):
    schema_version: Literal[1] = 1


class JobResponse(CheckJob):
    schema_version: Literal[1] = 1


class SuccessResponse(BaseModel):
    schema_version: Literal[1] = 1
    member_id: str
    event_id: str
    server_time: int
    expires_at: int


class HeartbeatResponse(BaseModel):
    schema_version: Literal[1] = 1
    member_id: str
    received_at: int


def _case_response(snapshot: CaseSnapshot) -> CaseResponse:
    return CaseResponse.model_validate(snapshot.model_dump())


@router.post("/success", response_model=SuccessResponse)
async def publish_success(
    body: SuccessRequest,
    principal: Annotated[ApiPrincipal, Depends(enforce_ownership_limit)],
    store: Annotated[OwnershipStore, Depends(get_ownership_store)],
) -> SuccessResponse:
    assert principal.member_id is not None
    try:
        receipt = await store.register_success(body.cycle, str(body.event_id), principal.member_id)
    except SuccessConflict as error:
        await store.record_conflict()
        raise HTTPException(status_code=409, detail="conflicting success receipt") from error
    except Exception as error:
        raise HTTPException(status_code=503, detail="ownership service unavailable") from error
    return SuccessResponse(
        member_id=receipt.member_id,
        event_id=receipt.event_id,
        server_time=receipt.server_time,
        expires_at=receipt.expires_at,
    )


@router.post("/cases", response_model=CaseResponse)
async def create_case(
    body: CaseRequest,
    principal: Annotated[ApiPrincipal, Depends(enforce_ownership_limit)],
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[OwnershipStore, Depends(get_ownership_store)],
) -> CaseResponse:
    assert principal.member_id is not None
    members = tuple(settings.ownership.members)
    try:
        snapshot = await store.open_case(
            str(body.case_id), body.cycle, members, creator_member=principal.member_id
        )
    except ValueError as error:
        await store.record_conflict()
        raise HTTPException(status_code=409, detail="conflicting ownership case") from error
    except Exception as error:
        raise HTTPException(status_code=503, detail="ownership service unavailable") from error
    return _case_response(snapshot)


@router.get("/cases/{case_id}", response_model=CaseResponse)
async def read_case(
    case_id: UUID4,
    schema_version: Annotated[int, Query(ge=1, le=1)],
    principal: Annotated[ApiPrincipal, Depends(enforce_ownership_limit)],
    store: Annotated[OwnershipStore, Depends(get_ownership_store)],
) -> CaseResponse:
    del schema_version
    try:
        snapshot = await store.read_case(str(case_id))
    except LookupError as error:
        raise HTTPException(status_code=404, detail="ownership case not found") from error
    except Exception as error:
        raise HTTPException(status_code=503, detail="ownership service unavailable") from error
    if principal.member_id not in snapshot.members:
        raise HTTPException(status_code=403, detail="case member required")
    return _case_response(snapshot)


@router.get("/jobs/next", response_model=JobResponse | None)
async def claim_job(
    schema_version: Annotated[int, Query(ge=1, le=1)],
    principal: Annotated[ApiPrincipal, Depends(enforce_ownership_limit)],
    store: Annotated[OwnershipStore, Depends(get_ownership_store)],
) -> JobResponse | None:
    del schema_version
    assert principal.member_id is not None
    try:
        job = await store.claim_job(principal.member_id)
    except Exception as error:
        raise HTTPException(status_code=503, detail="ownership service unavailable") from error
    return JobResponse.model_validate(job.model_dump()) if job is not None else None


@router.post("/checks", response_model=CaseResponse)
async def submit_check(
    body: CheckRequest,
    principal: Annotated[ApiPrincipal, Depends(enforce_ownership_limit)],
    store: Annotated[OwnershipStore, Depends(get_ownership_store)],
) -> CaseResponse:
    assert principal.member_id is not None
    try:
        snapshot = await store.submit_result(
            str(body.case_id),
            principal.member_id,
            body.round_no,
            body.result,
            str(body.result_id),
            body.lease_token,
        )
    except ValueError as error:
        await store.record_conflict()
        raise HTTPException(status_code=409, detail="conflicting ownership check") from error
    except Exception as error:
        raise HTTPException(status_code=503, detail="ownership service unavailable") from error
    return _case_response(snapshot)


@router.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    body: HeartbeatRequest,
    principal: Annotated[ApiPrincipal, Depends(enforce_ownership_limit)],
    store: Annotated[OwnershipStore, Depends(get_ownership_store)],
) -> HeartbeatResponse:
    assert principal.member_id is not None
    try:
        received_at = await store.heartbeat(principal.member_id, body.success_backlog)
    except Exception as error:
        raise HTTPException(status_code=503, detail="ownership service unavailable") from error
    return HeartbeatResponse(member_id=principal.member_id, received_at=received_at)
