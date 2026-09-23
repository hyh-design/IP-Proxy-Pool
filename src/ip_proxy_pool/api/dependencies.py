import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from prometheus_client import CollectorRegistry
from redis.asyncio import Redis

from ip_proxy_pool.api.cursors import CursorCodec
from ip_proxy_pool.config import Settings
from ip_proxy_pool.dashboard.heartbeat import WorkerHeartbeatStore
from ip_proxy_pool.dashboard.history import DashboardHistoryStore
from ip_proxy_pool.dashboard.service import DashboardService
from ip_proxy_pool.observability.metrics import Metrics, NoopMetrics, PrometheusMetrics
from ip_proxy_pool.reclaim_quota.store import ReclaimQuotaStore
from ip_proxy_pool.security.auth import (
    ApiPrincipal,
    AuthenticationError,
    KeyRole,
    authenticate_key,
)
from ip_proxy_pool.security.rate_limit import RedisRateLimiter
from ip_proxy_pool.storage.repository import RedisRepository


def build_lifespan(
    settings: Settings,
) -> Callable[[FastAPI], Any]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.validate_api_startup()
        redis = Redis.from_url(
            str(settings.redis.url),
            decode_responses=True,
            socket_connect_timeout=settings.redis.connect_timeout_seconds,
            socket_timeout=settings.redis.read_timeout_seconds,
        )
        app.state.settings = settings
        app.state.redis = redis
        app.state.repository = RedisRepository(
            redis,
            prefix=settings.redis.key_prefix,
            priority_max_latency_ms=settings.selection.max_latency_ms,
        )
        app.state.rate_limiter = RedisRateLimiter(redis, prefix=settings.redis.key_prefix)
        app.state.reclaim_quota_store = ReclaimQuotaStore(redis, prefix=settings.redis.key_prefix)
        cursor_secret = cast(Any, settings.api.cursor_secret).get_secret_value()
        app.state.cursor_codec = CursorCodec(secret=cursor_secret.encode())
        history = DashboardHistoryStore(
            redis,
            prefix=settings.redis.key_prefix,
            short_hours=settings.dashboard.short_retention_hours,
            long_hours=settings.dashboard.long_retention_hours,
        )
        heartbeat = WorkerHeartbeatStore(
            redis,
            prefix=settings.redis.key_prefix,
            interval_seconds=settings.dashboard.heartbeat_interval_seconds,
            ttl_seconds=settings.dashboard.heartbeat_ttl_seconds,
        )
        app.state.dashboard_service = DashboardService(
            redis,
            repository=app.state.repository,
            history=history,
            heartbeat=heartbeat,
            prefix=settings.redis.key_prefix,
            settings=settings.dashboard,
            selection=settings.selection,
        )
        if settings.observability.metrics_enabled:
            registry = CollectorRegistry()
            app.state.metrics_registry = registry
            app.state.metrics = PrometheusMetrics(registry=registry)
        else:
            app.state.metrics = NoopMetrics()
        try:
            yield
        finally:
            await redis.aclose()

    return lifespan


def _state(request: Request, name: str) -> Any:
    value = getattr(request.app.state, name, None)
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="service unavailable",
        )
    return value


def get_settings(request: Request) -> Settings:
    return cast(Settings, _state(request, "settings"))


def get_repository(request: Request) -> RedisRepository:
    return cast(RedisRepository, _state(request, "repository"))


def get_rate_limiter(request: Request) -> RedisRateLimiter:
    return cast(RedisRateLimiter, _state(request, "rate_limiter"))


def get_cursor_codec(request: Request) -> CursorCodec:
    return cast(CursorCodec, _state(request, "cursor_codec"))


def get_redis(request: Request) -> Any:
    return _state(request, "redis")


def get_dashboard_service(request: Request) -> DashboardService:
    return cast(DashboardService, _state(request, "dashboard_service"))


def get_reclaim_quota_store(request: Request) -> ReclaimQuotaStore:
    return cast(ReclaimQuotaStore, _state(request, "reclaim_quota_store"))


def get_metrics(request: Request) -> Metrics:
    return cast(Metrics, _state(request, "metrics"))


async def get_principal(
    settings: Annotated[Settings, Depends(get_settings)],
    api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> ApiPrincipal:
    if not settings.api.auth_enabled:
        return ApiPrincipal(role=KeyRole.ADMIN, fingerprint="auth-disabled")
    normal_keys = tuple(item.get_secret_value() for item in settings.api.api_keys)
    admin_keys = tuple(item.get_secret_value() for item in settings.api.admin_api_keys)
    quota_keys = tuple(item.get_secret_value() for item in settings.reclaim_quota.api_keys)
    try:
        return authenticate_key(
            api_key, normal_keys=normal_keys, admin_keys=admin_keys, quota_keys=quota_keys
        )
    except AuthenticationError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid API key",
        ) from error


async def require_admin(
    principal: Annotated[ApiPrincipal, Depends(get_principal)],
) -> ApiPrincipal:
    if principal.role is not KeyRole.ADMIN:
        raise HTTPException(status_code=403, detail="admin role required")
    return principal


async def require_quota_client(
    principal: Annotated[ApiPrincipal, Depends(get_principal)],
) -> ApiPrincipal:
    if principal.role is not KeyRole.QUOTA_CLIENT:
        raise HTTPException(status_code=403, detail="quota client role required")
    return principal


async def enforce_query_limit(
    settings: Annotated[Settings, Depends(get_settings)],
    limiter: Annotated[RedisRateLimiter, Depends(get_rate_limiter)],
    principal: Annotated[ApiPrincipal, Depends(get_principal)],
) -> ApiPrincipal:
    if principal.role is KeyRole.QUOTA_CLIENT:
        raise HTTPException(status_code=403, detail="query role required")
    try:
        decision = await limiter.check(
            "query",
            principal.fingerprint,
            settings.api.query_rate_limit,
            settings.api.rate_window_seconds,
            now=time.time(),
        )
    except Exception as error:
        raise HTTPException(status_code=503, detail="service unavailable") from error
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )
    return principal


async def enforce_admin_limit(
    settings: Annotated[Settings, Depends(get_settings)],
    limiter: Annotated[RedisRateLimiter, Depends(get_rate_limiter)],
    principal: Annotated[ApiPrincipal, Depends(require_admin)],
) -> ApiPrincipal:
    try:
        decision = await limiter.check(
            "admin",
            principal.fingerprint,
            settings.api.admin_rate_limit,
            settings.api.rate_window_seconds,
            now=time.time(),
        )
    except Exception as error:
        raise HTTPException(status_code=503, detail="service unavailable") from error
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )
    return principal
