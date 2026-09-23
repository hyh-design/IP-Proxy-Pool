"""Bounded formal-only peer synchronization worker."""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import ValidationError

from ip_proxy_pool.peer_cache.models import PeerCacheRecord, PeerExportResponse
from ip_proxy_pool.peer_cache.store import PeerCacheStore

MAX_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class PeerSyncOutcome:
    outcome: str
    accepted: int
    generation: int | None


class PeerSyncWorker:
    def __init__(
        self,
        store: PeerCacheStore,
        *,
        base_url: str,
        api_key: str,
        peer_name: str,
        origin_node: str,
        domain: str,
        client: httpx.AsyncClient | None = None,
        enabled: bool = True,
        interval_seconds: int = 60,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "peer-tunnel"
            or parsed.port != 8000
            or parsed.path not in {"", "/"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("peer sync URL must be the internal tunnel")
        if not peer_name or not origin_node or not domain or not api_key:
            raise ValueError("peer sync identity and key required")
        if interval_seconds < 10:
            raise ValueError("peer sync interval must be at least 10 seconds")
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.peer_name = peer_name
        self.origin_node = origin_node
        self.domain = domain
        self.enabled = enabled
        self.interval_seconds = interval_seconds
        self.now = now
        self.client = client or httpx.AsyncClient(
            follow_redirects=False, trust_env=False, timeout=3.0
        )
        self._owns_client = client is None

    async def sync_once(self, now: datetime) -> PeerSyncOutcome:
        if not self.enabled:
            return PeerSyncOutcome("disabled", 0, None)
        owner = uuid4().hex
        try:
            generation = await self.store.begin_sync(owner)
        except Exception:
            return PeerSyncOutcome("store_error", 0, None)
        if generation is None:
            return PeerSyncOutcome("lock_busy", 0, None)
        committed = False
        outcome = "store_error"
        accepted = 0
        try:
            try:
                async with asyncio.timeout(3):
                    async with self.client.stream(
                        "GET",
                        f"{self.base_url}/v1/peer/proxies",
                        params={
                            "domain": self.domain,
                            "count": 20,
                            "min_score": 90,
                            "max_latency_ms": 2000,
                            "max_checked_age_seconds": 600,
                            "min_consecutive_successes": 2,
                        },
                        headers={"X-API-Key": self.api_key},
                        follow_redirects=False,
                    ) as response:
                        if response.status_code in {401, 403}:
                            outcome = "auth_error"
                        elif 300 <= response.status_code < 400:
                            outcome = "redirect_error"
                        elif response.status_code != 200:
                            outcome = "http_error"
                        else:
                            chunks = bytearray()
                            async for chunk in response.aiter_bytes():
                                chunks.extend(chunk)
                                if len(chunks) > MAX_RESPONSE_BYTES:
                                    outcome = "oversize"
                                    break
                            if outcome != "oversize":
                                snapshot = PeerExportResponse.model_validate_json(bytes(chunks))
                                if (
                                    snapshot.origin_node != self.origin_node
                                    or abs((now - snapshot.generated_at).total_seconds()) > 5
                                ):
                                    outcome = "protocol_error"
                                else:
                                    endpoints: set[str] = set()
                                    records: list[PeerCacheRecord] = []
                                    for item in snapshot.items:
                                        if item.domain != self.domain or item.endpoint in endpoints:
                                            raise ValueError("invalid peer snapshot")
                                        endpoints.add(item.endpoint)
                                        records.append(
                                            PeerCacheRecord.model_validate(
                                                {
                                                    **item.model_dump(mode="json"),
                                                    "peer_name": self.peer_name,
                                                    "origin_node": self.origin_node,
                                                    "synced_at": now,
                                                    "expires_at": min(
                                                        now + timedelta(seconds=180),
                                                        item.last_checked_at
                                                        + timedelta(seconds=595),
                                                    ),
                                                }
                                            )
                                        )
                                    replacement = await self.store.replace(
                                        tuple(records), generation, owner, now
                                    )
                                    if replacement.stale_generation:
                                        outcome = "stale_generation"
                                    else:
                                        outcome = "success"
                                        accepted = replacement.accepted
                                        committed = True
            except (ValidationError, ValueError, UnicodeDecodeError):
                outcome = "protocol_error"
            except (httpx.TimeoutException, TimeoutError):
                outcome = "timeout"
            except httpx.RequestError:
                outcome = "network_error"
            except Exception:
                outcome = "store_error"
            if outcome != "success":
                try:
                    await self.store.note_failure(outcome, now)
                except Exception:
                    outcome = "store_error"
            return PeerSyncOutcome(outcome, accepted, generation)
        finally:
            if not committed:
                abort = getattr(self.store, "abort_sync", None)
                if abort is not None:
                    with suppress(Exception):
                        await abort(generation, owner)

    async def run(self, stop_event: asyncio.Event) -> None:
        try:
            while not stop_event.is_set():
                current = self.now()
                await self.sync_once(current)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
                except TimeoutError:
                    continue
        finally:
            if self._owns_client:
                await self.client.aclose()
