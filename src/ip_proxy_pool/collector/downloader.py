import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

_RETRY_STATUSES = {429, 502, 503, 504}


class ResponseTooLarge(ValueError):
    """Raised before retaining a remote body larger than the configured cap."""


@dataclass(frozen=True, slots=True)
class DownloadResult:
    payload: bytes
    content_type: str | None
    fetched_at: datetime


class SourceDownloader:
    def __init__(
        self,
        *,
        max_bytes: int,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
        attempts: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_bytes < 1 or timeout <= 0 or attempts < 1:
            raise ValueError("download limits must be positive")
        self._max_bytes = max_bytes
        self._attempts = attempts
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch(self, url: str) -> DownloadResult:
        for attempt in range(1, self._attempts + 1):
            retry_status = False
            try:
                async with self._client.stream("GET", url) as response:
                    if response.status_code in _RETRY_STATUSES:
                        if attempt == self._attempts:
                            response.raise_for_status()
                        retry_status = True
                    else:
                        response.raise_for_status()

                    if not retry_status:
                        content_length = response.headers.get("content-length")
                        if content_length is not None:
                            try:
                                declared_size = int(content_length)
                            except ValueError:
                                declared_size = 0
                            if declared_size > self._max_bytes:
                                raise ResponseTooLarge(
                                    "source response exceeds configured byte limit"
                                )

                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > self._max_bytes:
                                raise ResponseTooLarge(
                                    "source response exceeds configured byte limit"
                                )
                            chunks.append(chunk)
                        return DownloadResult(
                            payload=b"".join(chunks),
                            content_type=response.headers.get("content-type"),
                            fetched_at=datetime.now(UTC),
                        )
            except (httpx.ConnectTimeout, httpx.ReadTimeout):
                if attempt == self._attempts:
                    raise

            if retry_status or attempt < self._attempts:
                await self._sleep(float(2 ** (attempt - 1)))

        raise RuntimeError("source download exhausted retries")
