from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException

from ip_proxy_pool.api.dependencies import enforce_admin_limit
from ip_proxy_pool.api.models import AdminProbeRequest, AdminProbeResponse
from ip_proxy_pool.checker.errors import ProbeCategory
from ip_proxy_pool.config import Settings
from ip_proxy_pool.models import ProxyEndpoint
from ip_proxy_pool.security.auth import ApiPrincipal
from ip_proxy_pool.security.network import (
    system_resolver,
    validate_proxy_endpoint,
    validate_public_url,
)

Resolver = Callable[[str], Awaitable[set[str]]]


def _textual(content_type: str) -> bool:
    media_type = content_type.lower().split(";", maxsplit=1)[0].strip()
    return (
        media_type.startswith("text/")
        or media_type == "application/json"
        or media_type.endswith("+json")
        or media_type in {"application/xml", "application/xhtml+xml"}
        or media_type.endswith("+xml")
    )


def build_admin_router(
    settings: Settings,
    *,
    resolver: Resolver = system_resolver,
    transport: httpx.AsyncBaseTransport | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/v1/admin", tags=["admin"])

    @router.post("/probe", response_model=AdminProbeResponse)
    async def probe(
        body: AdminProbeRequest,
        _principal: Annotated[ApiPrincipal, Depends(enforce_admin_limit)],
    ) -> AdminProbeResponse:
        try:
            endpoint = validate_proxy_endpoint(ProxyEndpoint.parse(body.proxy))
            validated_url = await validate_public_url(
                body.url,
                allowed_hosts=settings.security.allowed_probe_hosts,
                resolver=resolver,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail="invalid probe boundary") from error

        started = perf_counter()
        proxy_url = f"http://{endpoint.canonical}"
        try:
            async with (
                httpx.AsyncClient(
                    proxy=proxy_url if transport is None else None,
                    transport=transport,
                    timeout=body.timeout_seconds,
                    trust_env=False,
                    follow_redirects=False,
                ) as client,
                client.stream("GET", validated_url.url) as response,
            ):
                content_type = response.headers.get("content-type", "")
                snippet: str | None = None
                if _textual(content_type):
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        remaining = max(0, 8192 - size)
                        chunks.append(chunk[:remaining])
                        size += min(len(chunk), remaining)
                        if size >= 8192:
                            break
                    snippet = b"".join(chunks).decode("utf-8", errors="replace")[:2048]
                return AdminProbeResponse(
                    category=ProbeCategory.SUCCESS,
                    latency_ms=(perf_counter() - started) * 1000,
                    status_code=response.status_code,
                    snippet=snippet,
                )
        except (httpx.ProxyError, httpx.ConnectTimeout):
            return AdminProbeResponse(category=ProbeCategory.PROXY_ERROR)
        except Exception:
            return AdminProbeResponse(category=ProbeCategory.SYSTEM_ERROR)

    return router
