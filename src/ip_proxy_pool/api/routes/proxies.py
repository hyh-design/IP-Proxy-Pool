from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from ip_proxy_pool.api.cursors import CursorCodec, InvalidCursor, query_filters_hash
from ip_proxy_pool.api.dependencies import (
    enforce_query_limit,
    get_cursor_codec,
    get_repository,
    get_settings,
)
from ip_proxy_pool.api.models import (
    DomainsResponse,
    FeedbackOutcome,
    ProxyFeedbackRequest,
    ProxyPageResponse,
    ProxyResponse,
    RandomProxyResponse,
)
from ip_proxy_pool.checker.errors import ProbeCategory, ProbeResult
from ip_proxy_pool.checker.scheduling import next_check_at
from ip_proxy_pool.checker.scoring import apply_probe_result
from ip_proxy_pool.config import Settings
from ip_proxy_pool.models import ProxyEndpoint, ProxyState
from ip_proxy_pool.security.auth import ApiPrincipal
from ip_proxy_pool.storage.repository import RedisRepository

router = APIRouter(prefix="/v1", tags=["proxies"])


async def _require_domain(repository: RedisRepository, domain: str) -> None:
    try:
        domains = await repository.list_domains()
    except Exception as error:
        raise HTTPException(status_code=503, detail="service unavailable") from error
    if domain not in domains:
        raise HTTPException(status_code=404, detail="domain not found")


@router.get("/domains", response_model=DomainsResponse)
async def domains(
    _principal: Annotated[ApiPrincipal, Depends(enforce_query_limit)],
    repository: Annotated[RedisRepository, Depends(get_repository)],
) -> DomainsResponse:
    try:
        return DomainsResponse(domains=await repository.list_domains())
    except Exception as error:
        raise HTTPException(status_code=503, detail="service unavailable") from error


@router.get("/proxies/random", response_model=RandomProxyResponse)
async def random_proxies(
    _principal: Annotated[ApiPrincipal, Depends(enforce_query_limit)],
    repository: Annotated[RedisRepository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    domain: Annotated[str, Query(min_length=1, max_length=253)],
    count: Annotated[int, Query(ge=1, le=20)] = 1,
    min_score: Annotated[int, Query(ge=0, le=100)] = 90,
    max_latency_ms: Annotated[float | None, Query(ge=0)] = None,
    max_checked_age_seconds: Annotated[int | None, Query(gt=0)] = None,
    min_consecutive_successes: Annotated[int | None, Query(ge=1, le=20)] = None,
) -> RandomProxyResponse:
    await _require_domain(repository, domain)
    selection = settings.selection
    try:
        records = await repository.random_proxies(
            domain=domain,
            min_score=max(min_score, selection.min_score),
            count=count,
            max_latency_ms=min(
                max_latency_ms if max_latency_ms is not None else selection.max_latency_ms,
                selection.max_latency_ms,
            ),
            max_checked_age_seconds=min(
                max_checked_age_seconds
                if max_checked_age_seconds is not None
                else selection.max_checked_age_seconds,
                selection.max_checked_age_seconds,
            ),
            min_consecutive_successes=max(
                min_consecutive_successes
                if min_consecutive_successes is not None
                else selection.min_consecutive_successes,
                selection.min_consecutive_successes,
            ),
        )
    except Exception as error:
        raise HTTPException(status_code=503, detail="service unavailable") from error
    return RandomProxyResponse(items=[ProxyResponse.from_record(record) for record in records])


@router.post("/proxies/feedback", response_model=ProxyResponse)
async def proxy_feedback(
    body: ProxyFeedbackRequest,
    _principal: Annotated[ApiPrincipal, Depends(enforce_query_limit)],
    repository: Annotated[RedisRepository, Depends(get_repository)],
) -> ProxyResponse:
    await _require_domain(repository, body.domain)
    try:
        endpoint = ProxyEndpoint.parse(body.endpoint).canonical
        record = await repository.get_record(body.domain, endpoint)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="invalid proxy endpoint") from error
    except Exception as error:
        raise HTTPException(status_code=503, detail="service unavailable") from error
    if record is None:
        raise HTTPException(status_code=404, detail="proxy not found")

    category = (
        ProbeCategory.SUCCESS
        if body.outcome is FeedbackOutcome.SUCCESS
        else ProbeCategory.PROXY_ERROR
    )
    now = datetime.now(UTC)
    result = ProbeResult(
        category=category,
        latency_ms=body.latency_ms,
        status_code=body.status_code,
        error_type=body.error_type,
    )
    updated = apply_probe_result(record, result, now=now)
    if body.outcome is FeedbackOutcome.PROXY_ERROR:
        updated = updated.model_copy(update={"state": ProxyState.QUARANTINED})
    updated = updated.model_copy(update={"next_check_at": next_check_at(updated, now=now)})
    try:
        await repository.save_record(updated)
    except Exception as error:
        raise HTTPException(status_code=503, detail="service unavailable") from error
    return ProxyResponse.from_record(updated)


@router.get("/proxies", response_model=ProxyPageResponse)
async def list_proxies(
    _principal: Annotated[ApiPrincipal, Depends(enforce_query_limit)],
    repository: Annotated[RedisRepository, Depends(get_repository)],
    codec: Annotated[CursorCodec, Depends(get_cursor_codec)],
    domain: Annotated[str, Query(min_length=1, max_length=253)],
    state: Annotated[ProxyState | None, Query()] = None,
    min_score: Annotated[int, Query(ge=0, le=100)] = 0,
    max_score: Annotated[int, Query(ge=0, le=100)] = 100,
    source: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=1024)] = None,
) -> ProxyPageResponse:
    if min_score > max_score:
        raise HTTPException(status_code=422, detail="min_score must not exceed max_score")
    await _require_domain(repository, domain)
    filters_hash = query_filters_hash(state, min_score, max_score, source, limit)
    offset = 0
    if cursor is not None:
        try:
            offset = codec.decode(
                cursor,
                expected_domain=domain,
                expected_filters_hash=filters_hash,
            ).offset
        except InvalidCursor as error:
            raise HTTPException(status_code=422, detail="invalid cursor") from error
    try:
        page = await repository.list_proxies(
            domain=domain,
            min_score=min_score,
            limit=limit,
            offset=offset,
            state=state,
            max_score=max_score,
            source=source,
        )
    except Exception as error:
        raise HTTPException(status_code=503, detail="service unavailable") from error
    next_cursor = (
        codec.encode(
            domain=domain,
            offset=page.next_offset,
            filters_hash=filters_hash,
        )
        if page.next_offset is not None
        else None
    )
    return ProxyPageResponse(
        items=[ProxyResponse.from_record(record) for record in page.items],
        next_cursor=next_cursor,
    )
