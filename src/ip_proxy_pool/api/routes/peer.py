"""Dedicated, formal-only peer export endpoint."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from ip_proxy_pool.api.dependencies import (
    enforce_peer_export_limit,
    get_repository,
    get_settings,
)
from ip_proxy_pool.config import Settings
from ip_proxy_pool.models import ProxyState
from ip_proxy_pool.peer_cache.models import PeerExportItem, PeerExportResponse
from ip_proxy_pool.peer_cache.policy import SelectionPolicy, accepts, effective_peer_policy
from ip_proxy_pool.security.auth import ApiPrincipal
from ip_proxy_pool.storage.repository import LatencyIndexNotReadyError, RedisRepository

router = APIRouter(prefix="/v1/peer", tags=["peer-export"])


@router.get("/proxies", response_model=PeerExportResponse)
async def export_proxies(
    _principal: Annotated[ApiPrincipal, Depends(enforce_peer_export_limit)],
    repository: Annotated[RedisRepository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    domain: Annotated[str, Query(min_length=1, max_length=253)],
    count: Annotated[int, Query(ge=1, le=20)] = 20,
    min_score: Annotated[int, Query(ge=0, le=100)] = 90,
    max_latency_ms: Annotated[float, Query(ge=0, allow_inf_nan=False)] = 2000,
    max_checked_age_seconds: Annotated[int, Query(gt=0)] = 600,
    min_consecutive_successes: Annotated[int, Query(ge=1, le=20)] = 2,
) -> PeerExportResponse:
    if not settings.peer_export.enabled:
        raise HTTPException(status_code=503, detail="peer export unavailable")
    if domain != settings.target.domain:
        raise HTTPException(status_code=422, detail="unsupported domain")
    policy = effective_peer_policy(
        SelectionPolicy(
            domain, min_score, max_latency_ms, max_checked_age_seconds, min_consecutive_successes
        ),
        SelectionPolicy(
            domain,
            settings.selection.min_score,
            settings.selection.max_latency_ms,
            settings.selection.max_checked_age_seconds,
            settings.selection.min_consecutive_successes,
        ),
        SelectionPolicy(
            domain,
            settings.peer_cache.min_score,
            settings.peer_cache.max_latency_ms,
            settings.peer_cache.max_checked_age_seconds,
            settings.peer_cache.min_consecutive_successes,
        ),
    )
    now = datetime.now(UTC)
    try:
        result = await repository.select_formal_export(
            domain=domain,
            min_score=policy.min_score,
            count=count,
            max_latency_ms=policy.max_latency_ms,
            max_checked_age_seconds=policy.max_checked_age_seconds,
            min_consecutive_successes=policy.min_consecutive_successes,
            now=now,
        )
    except LatencyIndexNotReadyError as error:
        raise HTTPException(status_code=503, detail="latency index not ready") from error
    except Exception as error:
        raise HTTPException(status_code=503, detail="service unavailable") from error
    items: list[PeerExportItem] = []
    for record in result.records:
        if record.state is not ProxyState.AVAILABLE or not accepts(record, policy, now):
            continue
        try:
            items.append(PeerExportItem.from_record(record))
        except ValueError:
            continue
    return PeerExportResponse(
        origin_node=settings.peer_export.node_id, generated_at=now, items=tuple(items[:count])
    )
