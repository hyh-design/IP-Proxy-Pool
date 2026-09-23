import time
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ip_proxy_pool.api.cursors import CursorCodec, InvalidCursor, query_filters_hash
from ip_proxy_pool.api.dependencies import (
    enforce_query_limit,
    get_cursor_codec,
    get_metrics,
    get_repository,
    get_settings,
)
from ip_proxy_pool.api.models import (
    DomainsResponse,
    FeedbackOutcome,
    PeerFeedbackResponse,
    PeerRandomProxyResponse,
    ProxyFeedbackRequest,
    ProxyPageResponse,
    ProxyResponse,
    RandomProxyResponse,
    SelectedProxyResponse,
)
from ip_proxy_pool.checker.errors import ProbeCategory, ProbeResult
from ip_proxy_pool.checker.scheduling import next_check_at
from ip_proxy_pool.checker.scoring import apply_probe_result
from ip_proxy_pool.config import Settings
from ip_proxy_pool.models import ProxyEndpoint, ProxyState
from ip_proxy_pool.observability.metrics import Metrics
from ip_proxy_pool.peer_cache.models import PeerExportItem
from ip_proxy_pool.peer_cache.policy import SelectionPolicy, effective_peer_policy
from ip_proxy_pool.peer_cache.receipts import (
    InvalidReceipt,
    ReceiptBindingError,
    SelectionReceiptStore,
)
from ip_proxy_pool.peer_cache.store import PeerCacheStore
from ip_proxy_pool.security.auth import ApiPrincipal
from ip_proxy_pool.storage.repository import LatencyIndexNotReadyError, RedisRepository

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


@router.get("/proxies/random", response_model=RandomProxyResponse | PeerRandomProxyResponse)
async def random_proxies(
    request: Request,
    _principal: Annotated[ApiPrincipal, Depends(enforce_query_limit)],
    repository: Annotated[RedisRepository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    metrics: Annotated[Metrics, Depends(get_metrics)],
    domain: Annotated[str, Query(min_length=1, max_length=253)],
    count: Annotated[int, Query(ge=1, le=20)] = 1,
    min_score: Annotated[int, Query(ge=0, le=100)] = 90,
    max_latency_ms: Annotated[float | None, Query(ge=0, allow_inf_nan=False)] = None,
    max_checked_age_seconds: Annotated[int | None, Query(gt=0)] = None,
    min_consecutive_successes: Annotated[int | None, Query(ge=1, le=20)] = None,
    include_peer_cache: bool = False,
) -> RandomProxyResponse | PeerRandomProxyResponse:
    await _require_domain(repository, domain)
    selection = settings.selection
    try:
        started = time.perf_counter()
        result = await repository.select_random_proxies(
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
    except LatencyIndexNotReadyError as error:
        raise HTTPException(status_code=503, detail="latency index not ready") from error
    except Exception as error:
        raise HTTPException(status_code=503, detail="service unavailable") from error
    requested = min(count, 20)
    metrics.proxy_selection(
        domain,
        index_members=result.index_members,
        candidates=result.indexed_candidates,
        requested=requested,
        returned=len(result.records),
        skipped={
            "score": result.skipped_score,
            "freshness": result.skipped_freshness,
            "successes": result.skipped_successes,
            "inconsistent": result.skipped_inconsistent,
        },
        duration=time.perf_counter() - started,
    )
    formal_response = RandomProxyResponse(
        items=[ProxyResponse.from_record(record) for record in result.records]
    )
    if not (
        include_peer_cache and settings.peer_cache.enabled and domain == settings.target.domain
    ):
        return formal_response

    receipts: SelectionReceiptStore | None = getattr(request.app.state, "selection_receipts", None)
    cache: PeerCacheStore | None = getattr(request.app.state, "peer_cache_store", None)
    if receipts is None or cache is None:
        return formal_response
    now = datetime.now(UTC)
    selected: list[SelectedProxyResponse] = []
    formal_max_age = min(
        max_checked_age_seconds or selection.max_checked_age_seconds,
        selection.max_checked_age_seconds,
    )
    try:
        for record in result.records:
            snapshot = PeerExportItem.from_record(record)
            token = await receipts.issue(snapshot, _principal.fingerprint, now)
            usable_until = min(
                now.timestamp() + 600,
                snapshot.last_checked_at.timestamp() + formal_max_age - 5,
            )
            if usable_until <= now.timestamp():
                return formal_response
            selected.append(
                SelectedProxyResponse(
                    **ProxyResponse.from_record(record).model_dump(),
                    selection_source="formal",
                    peer_name=None,
                    selection_token=token,
                    usable_until=datetime.fromtimestamp(usable_until, UTC),
                )
            )
        if len(selected) < count:
            cache_settings = settings.peer_cache
            policy = effective_peer_policy(
                SelectionPolicy(
                    domain,
                    min_score,
                    max_latency_ms if max_latency_ms is not None else selection.max_latency_ms,
                    max_checked_age_seconds
                    if max_checked_age_seconds is not None
                    else selection.max_checked_age_seconds,
                    min_consecutive_successes
                    if min_consecutive_successes is not None
                    else selection.min_consecutive_successes,
                ),
                SelectionPolicy(
                    domain,
                    selection.min_score,
                    selection.max_latency_ms,
                    selection.max_checked_age_seconds,
                    selection.min_consecutive_successes,
                ),
                SelectionPolicy(
                    domain,
                    cache_settings.min_score,
                    cache_settings.max_latency_ms,
                    cache_settings.max_checked_age_seconds,
                    cache_settings.min_consecutive_successes,
                ),
            )
            exclusions = tuple(record.endpoint.canonical for record in result.records)
            peer_records = await cache.select(
                policy, count - len(selected), exclusions, _principal.fingerprint, now
            )
            for peer in peer_records:
                if peer.endpoint in exclusions:
                    continue
                selected.append(
                    SelectedProxyResponse(
                        endpoint=peer.endpoint,
                        domain=peer.domain,
                        score=peer.score,
                        state=None,
                        source_names=list(peer.source_names),
                        latency_ewma_ms=peer.latency_ewma_ms,
                        last_checked_at=peer.last_checked_at,
                        next_check_at=None,
                        selection_source="peer",
                        peer_name=peer.peer_name,
                        selection_token=peer.selection_token,
                        usable_until=peer.usable_until,
                    )
                )
    except Exception:
        return formal_response
    return PeerRandomProxyResponse(items=selected[:count])


@router.post("/proxies/feedback", response_model=ProxyResponse | PeerFeedbackResponse)
async def proxy_feedback(
    request: Request,
    body: ProxyFeedbackRequest,
    _principal: Annotated[ApiPrincipal, Depends(enforce_query_limit)],
    repository: Annotated[RedisRepository, Depends(get_repository)],
) -> ProxyResponse | PeerFeedbackResponse:
    if body.selection_token is None:
        await _require_domain(repository, body.domain)
    try:
        endpoint = ProxyEndpoint.parse(body.endpoint).canonical
    except ValueError as error:
        raise HTTPException(status_code=422, detail="invalid proxy endpoint") from error
    if body.selection_token is not None:
        receipts: SelectionReceiptStore | None = getattr(
            request.app.state, "selection_receipts", None
        )
        if receipts is None:
            raise HTTPException(status_code=503, detail="service unavailable")
        now = datetime.now(UTC)
        try:
            receipt = await receipts.resolve(
                body.selection_token, _principal.fingerprint, body.domain, endpoint, now
            )
        except InvalidReceipt as error:
            raise HTTPException(status_code=409, detail="invalid selection receipt") from error
        except ReceiptBindingError as error:
            raise HTTPException(
                status_code=403, detail="selection receipt binding mismatch"
            ) from error
        except Exception as error:
            raise HTTPException(status_code=503, detail="service unavailable") from error
        if receipt.selection_source == "peer":
            cache: PeerCacheStore | None = getattr(request.app.state, "peer_cache_store", None)
            if cache is None:
                redis = getattr(request.app.state, "redis", None)
                if redis is None or receipt.peer_name is None:
                    raise HTTPException(status_code=503, detail="service unavailable")
                cache = PeerCacheStore(
                    redis,
                    prefix=request.app.state.settings.redis.key_prefix,
                    peer_name=receipt.peer_name,
                    domain=body.domain,
                )
            if body.outcome is FeedbackOutcome.PROXY_ERROR:
                try:
                    await cache.invalidate(receipt, now)
                except Exception as error:
                    raise HTTPException(status_code=503, detail="service unavailable") from error
            snapshot = receipt.snapshot
            return PeerFeedbackResponse(
                endpoint=snapshot["endpoint"],
                domain=snapshot["domain"],
                score=snapshot["score"],
                source_names=list(snapshot["source_names"]),
                latency_ewma_ms=snapshot["latency_ewma_ms"],
                last_checked_at=datetime.fromisoformat(snapshot["last_checked_at"]),
                peer_name=receipt.peer_name or "",
            )
        if body.outcome is FeedbackOutcome.PROXY_ERROR:
            try:
                first_failure = await receipts.claim_formal_failure(receipt)
            except Exception as error:
                raise HTTPException(status_code=503, detail="service unavailable") from error
            if not first_failure:
                try:
                    record = await repository.get_record(body.domain, endpoint)
                except Exception as error:
                    raise HTTPException(status_code=503, detail="service unavailable") from error
                if record is None:
                    raise HTTPException(status_code=404, detail="proxy not found")
                return ProxyResponse.from_record(record)
    try:
        record = await repository.get_record(body.domain, endpoint)
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
