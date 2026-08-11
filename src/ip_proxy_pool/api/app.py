import re
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ip_proxy_pool.api.dependencies import build_lifespan
from ip_proxy_pool.api.routes.admin import build_admin_router
from ip_proxy_pool.api.routes.dashboard import router as dashboard_router
from ip_proxy_pool.api.routes.dashboard_page import (
    ASSET_DIRECTORY,
    DashboardStaticFiles,
)
from ip_proxy_pool.api.routes.dashboard_page import (
    router as dashboard_page_router,
)
from ip_proxy_pool.api.routes.health import router as health_router
from ip_proxy_pool.api.routes.legacy import build_legacy_router
from ip_proxy_pool.api.routes.proxies import router as proxies_router
from ip_proxy_pool.api.routes.stats import router as stats_router
from ip_proxy_pool.config import Settings, get_settings

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or get_settings()
    app = FastAPI(
        title="IP Proxy Pool",
        version="0.1.0",
        lifespan=build_lifespan(configured),
    )
    app.include_router(health_router)
    app.include_router(proxies_router)
    app.include_router(stats_router)
    if configured.dashboard.enabled:
        app.include_router(dashboard_router)
        app.include_router(dashboard_page_router)
        app.mount(
            "/dashboard/assets",
            DashboardStaticFiles(directory=ASSET_DIRECTORY),
            name="dashboard-assets",
        )
    if configured.api.admin_probe_enabled:
        app.include_router(build_admin_router(configured))
    if configured.api.legacy_routes_enabled:
        app.include_router(build_legacy_router())

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        supplied = request.headers.get("X-Request-ID", "")
        request_id = supplied if _REQUEST_ID.fullmatch(supplied) else uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, error: Exception) -> JSONResponse:
        del request, error
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    return app


app = create_app()
