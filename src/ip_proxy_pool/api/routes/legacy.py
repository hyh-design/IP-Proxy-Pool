from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response

from ip_proxy_pool.api.dependencies import enforce_query_limit, get_repository
from ip_proxy_pool.security.auth import ApiPrincipal
from ip_proxy_pool.storage.repository import RedisRepository

_SUNSET = "Fri, 31 Dec 2027 23:59:59 GMT"


def _deprecate(response: Response) -> None:
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = _SUNSET


async def _read(
    repository: RedisRepository,
    *,
    domain: str,
    limit: int,
) -> list[str]:
    try:
        if domain not in await repository.list_domains():
            raise HTTPException(status_code=404, detail="domain not found")
        page = await repository.list_proxies(
            domain=domain,
            min_score=0,
            limit=limit,
            offset=0,
        )
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=503, detail="service unavailable") from error
    return [record.endpoint.canonical for record in page.items]


def build_legacy_router() -> APIRouter:
    router = APIRouter(tags=["legacy"])

    @router.get("/", response_model=list[str])
    async def one(
        response: Response,
        _principal: Annotated[ApiPrincipal, Depends(enforce_query_limit)],
        repository: Annotated[RedisRepository, Depends(get_repository)],
        domain: Annotated[str, Query(min_length=1, max_length=253)],
    ) -> list[str]:
        _deprecate(response)
        return await _read(repository, domain=domain, limit=1)

    @router.get("/proxy/{num}", response_model=list[str])
    async def numbered(
        response: Response,
        _principal: Annotated[ApiPrincipal, Depends(enforce_query_limit)],
        repository: Annotated[RedisRepository, Depends(get_repository)],
        domain: Annotated[str, Query(min_length=1, max_length=253)],
        num: Annotated[int, Path(ge=1, le=200)],
    ) -> list[str]:
        _deprecate(response)
        return await _read(repository, domain=domain, limit=num)

    @router.get("/all", response_model=list[str])
    async def all_proxies(
        response: Response,
        _principal: Annotated[ApiPrincipal, Depends(enforce_query_limit)],
        repository: Annotated[RedisRepository, Depends(get_repository)],
        domain: Annotated[str, Query(min_length=1, max_length=253)],
    ) -> list[str]:
        _deprecate(response)
        return await _read(repository, domain=domain, limit=200)

    return router
