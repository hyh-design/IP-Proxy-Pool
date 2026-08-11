"""Authenticated, read-only dashboard JSON endpoints."""

import hashlib
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response

from ip_proxy_pool.api.dashboard_models import (
    DashboardSummary,
    QualitySummary,
    SourceSummary,
)
from ip_proxy_pool.api.dependencies import enforce_query_limit, get_dashboard_service
from ip_proxy_pool.dashboard.models import HistoryRange, HistorySeries
from ip_proxy_pool.dashboard.service import DashboardDomainNotFound, DashboardService
from ip_proxy_pool.security.auth import ApiPrincipal

router = APIRouter(prefix="/v1/dashboard", tags=["dashboard"])


def _not_found(error: DashboardDomainNotFound) -> HTTPException:
    return HTTPException(status_code=404, detail="domain not found")


def _unavailable(error: Exception) -> HTTPException:
    return HTTPException(status_code=503, detail="service unavailable")


@router.get("/summary", response_model=DashboardSummary)
async def summary(
    _principal: Annotated[ApiPrincipal, Depends(enforce_query_limit)],
    service: Annotated[DashboardService, Depends(get_dashboard_service)],
    domain: Annotated[str | None, Query(min_length=1, max_length=253)] = None,
) -> DashboardSummary:
    try:
        return await service.summary(domain, now=datetime.now(UTC))
    except DashboardDomainNotFound as error:
        raise _not_found(error) from error
    except Exception as error:
        raise _unavailable(error) from error


@router.get("/history", response_model=HistorySeries)
async def history(
    _principal: Annotated[ApiPrincipal, Depends(enforce_query_limit)],
    service: Annotated[DashboardService, Depends(get_dashboard_service)],
    response: Response,
    domain: Annotated[str | None, Query(min_length=1, max_length=253)] = None,
    range_: Annotated[HistoryRange, Query(alias="range")] = HistoryRange.H24,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> HistorySeries | Response:
    try:
        value = await service.history(domain, range_, now=datetime.now(UTC))
    except DashboardDomainNotFound as error:
        raise _not_found(error) from error
    except Exception as error:
        raise _unavailable(error) from error
    digest = hashlib.sha256(value.model_dump_json().encode()).hexdigest()
    etag = f'"{digest}"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return value


@router.get("/quality", response_model=QualitySummary)
async def quality(
    _principal: Annotated[ApiPrincipal, Depends(enforce_query_limit)],
    service: Annotated[DashboardService, Depends(get_dashboard_service)],
    domain: Annotated[str, Query(min_length=1, max_length=253)],
) -> QualitySummary:
    try:
        return await service.quality(domain, now=datetime.now(UTC))
    except DashboardDomainNotFound as error:
        raise _not_found(error) from error
    except Exception as error:
        raise _unavailable(error) from error


@router.get("/sources", response_model=SourceSummary)
async def sources(
    _principal: Annotated[ApiPrincipal, Depends(enforce_query_limit)],
    service: Annotated[DashboardService, Depends(get_dashboard_service)],
    domain: Annotated[str, Query(min_length=1, max_length=253)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SourceSummary:
    try:
        return await service.sources(domain, limit=limit, now=datetime.now(UTC))
    except DashboardDomainNotFound as error:
        raise _not_found(error) from error
    except Exception as error:
        raise _unavailable(error) from error
