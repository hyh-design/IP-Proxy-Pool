"""Self-contained dashboard page and hardened static asset serving."""

from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles

ASSET_DIRECTORY = Path(__file__).resolve().parents[2] / "static" / "dashboard"
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

router = APIRouter(include_in_schema=False)


class DashboardStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: MutableMapping[str, Any]) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "public, max-age=3600"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response


@router.get("/dashboard")
async def dashboard_page() -> FileResponse:
    return FileResponse(
        ASSET_DIRECTORY / "index.html",
        media_type="text/html",
        headers={**SECURITY_HEADERS, "Cache-Control": "no-store"},
    )
