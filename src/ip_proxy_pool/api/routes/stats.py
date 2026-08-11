import hashlib
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ip_proxy_pool.api.dependencies import (
    enforce_query_limit,
    get_redis,
    get_repository,
)
from ip_proxy_pool.api.models import StatsResponse
from ip_proxy_pool.security.auth import ApiPrincipal
from ip_proxy_pool.storage.repository import RedisRepository

router = APIRouter(prefix="/v1", tags=["stats"])


@router.get("/stats", response_model=StatsResponse)
async def stats(
    _principal: Annotated[ApiPrincipal, Depends(enforce_query_limit)],
    repository: Annotated[RedisRepository, Depends(get_repository)],
    redis: Annotated[Any, Depends(get_redis)],
    domain: Annotated[str | None, Query(min_length=1, max_length=253)] = None,
) -> StatsResponse:
    digest = hashlib.sha256((domain or "all").encode()).hexdigest()[:16]
    key = f"api:stats:{digest}"
    try:
        cached = await redis.get(key)
        if cached is not None:
            try:
                return StatsResponse.model_validate_json(cached)
            except ValueError:
                pass
        value = StatsResponse.model_validate((await repository.stats(domain)).model_dump())
        await redis.setex(key, 10, value.model_dump_json())
        return value
    except Exception as error:
        raise HTTPException(status_code=503, detail="service unavailable") from error
