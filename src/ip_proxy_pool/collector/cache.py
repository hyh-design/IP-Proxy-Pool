import base64
import hashlib
import json
from datetime import datetime
from typing import Any, cast

from ip_proxy_pool.collector.downloader import DownloadResult


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_cache_key(prefix: str, url: str) -> str:
    if not prefix or not url:
        raise ValueError("prefix and URL must be non-empty")
    return f"{prefix}:source-cache:page:{_digest(url)}"


class SourceCache:
    def __init__(self, redis: Any, *, prefix: str) -> None:
        if not prefix:
            raise ValueError("prefix must be non-empty")
        self._redis = redis
        self._prefix = prefix

    def _pagination_key(self, source_name: str) -> str:
        if not source_name:
            raise ValueError("source_name must be non-empty")
        return f"{self._prefix}:source-cache:pagination:{_digest(source_name)}"

    async def get_page(self, url: str) -> DownloadResult | None:
        key = source_cache_key(self._prefix, url)
        raw = cast(str | None, await self._redis.get(key))
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("cache payload must be an object")
            return DownloadResult(
                payload=base64.b64decode(value["payload"], validate=True),
                content_type=value.get("content_type"),
                fetched_at=datetime.fromisoformat(value["fetched_at"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            await self._redis.delete(key)
            return None

    async def set_page(
        self,
        url: str,
        result: DownloadResult,
        *,
        ttl: int,
    ) -> None:
        if ttl < 1:
            raise ValueError("ttl must be positive")
        value = json.dumps(
            {
                "payload": base64.b64encode(result.payload).decode("ascii"),
                "content_type": result.content_type,
                "fetched_at": result.fetched_at.isoformat(),
            },
            separators=(",", ":"),
        )
        await self._redis.setex(source_cache_key(self._prefix, url), ttl, value)

    async def get_pagination(self, source_name: str) -> list[str] | None:
        key = self._pagination_key(source_name)
        raw = cast(str | None, await self._redis.get(key))
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError("pagination cache must contain string URLs")
            return cast(list[str], value)
        except (TypeError, ValueError, json.JSONDecodeError):
            await self._redis.delete(key)
            return None

    async def set_pagination(
        self,
        source_name: str,
        pages: list[str],
        *,
        ttl: int,
    ) -> None:
        if ttl < 1:
            raise ValueError("ttl must be positive")
        await self._redis.setex(
            self._pagination_key(source_name),
            ttl,
            json.dumps(pages, separators=(",", ":")),
        )
