from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from ip_proxy_pool.api.dependencies import get_repository
from ip_proxy_pool.api.models import StatusResponse
from ip_proxy_pool.storage.repository import RedisRepository

router = APIRouter(tags=["health"])


@router.get("/health/live", response_model=StatusResponse)
async def live() -> StatusResponse:
    return StatusResponse(status="alive")


@router.get("/health/ready", response_model=StatusResponse)
async def ready(
    repository: Annotated[RedisRepository, Depends(get_repository)],
) -> StatusResponse:
    try:
        healthy = await repository.ping()
        indexes_ready = await repository.all_selection_indexes_ready()
    except Exception as error:
        raise HTTPException(status_code=503, detail="service unavailable") from error
    if not healthy:
        raise HTTPException(status_code=503, detail="service unavailable")
    if not indexes_ready:
        raise HTTPException(status_code=503, detail="selection indexes not ready")
    return StatusResponse(status="ready")


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    registry = getattr(request.app.state, "metrics_registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="metrics unavailable")
    return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
