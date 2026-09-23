"""Opaque selection receipts, bound to principal and exact selection context."""

import hashlib
import secrets
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from ip_proxy_pool.peer_cache.models import PeerCacheRecord, PeerExportItem
from ip_proxy_pool.storage.keys import peer_cache_keys_for


class InvalidReceipt(ValueError):
    """Unknown or expired receipt."""


class ReceiptBindingError(ValueError):
    """Receipt exists but belongs to a different caller or selection."""


class SelectionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    digest: str
    principal_fingerprint: str
    domain: str
    endpoint: str
    selection_source: Literal["formal", "peer"]
    peer_name: str | None
    source_checked_at: float
    snapshot: dict[str, Any]
    issued_at: float
    expires_at: float
    failure_applied: bool = False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SelectionReceiptStore:
    def __init__(self, redis: Any, *, prefix: str, domain: str, peer_name: str) -> None:
        self._redis = redis
        self._keys = peer_cache_keys_for(prefix, domain, peer_name)

    def key(self, digest: str) -> str:
        return self._keys.receipts_prefix + digest

    async def issue(
        self,
        snapshot: PeerExportItem | PeerCacheRecord,
        principal_fingerprint: str,
        now: datetime,
        *,
        selection_source: Literal["formal", "peer"] = "formal",
        peer_name: str | None = None,
    ) -> str:
        issued_at = now.timestamp()
        for _ in range(3):
            token = secrets.token_urlsafe(32)
            digest = token_digest(token)
            receipt = SelectionReceipt(
                digest=digest,
                principal_fingerprint=principal_fingerprint,
                domain=snapshot.domain,
                endpoint=snapshot.endpoint,
                selection_source=selection_source,
                peer_name=peer_name,
                source_checked_at=snapshot.last_checked_at.timestamp(),
                snapshot=snapshot.model_dump(mode="json"),
                issued_at=issued_at,
                expires_at=issued_at + 600,
            )
            if await self._redis.set(self.key(digest), receipt.model_dump_json(), ex=600, nx=True):
                return token
        raise RuntimeError("unable to issue selection receipt")

    async def resolve(
        self,
        token: str,
        principal_fingerprint: str,
        domain: str,
        endpoint: str,
        now: datetime,
    ) -> SelectionReceipt:
        if not token or len(token) > 256:
            raise InvalidReceipt("invalid selection receipt")
        raw = await self._redis.get(self.key(token_digest(token)))
        if raw is None:
            raise InvalidReceipt("invalid selection receipt")
        receipt = SelectionReceipt.model_validate_json(raw)
        if now.timestamp() >= receipt.expires_at:
            raise InvalidReceipt("expired selection receipt")
        if (
            not secrets.compare_digest(receipt.principal_fingerprint, principal_fingerprint)
            or receipt.domain != domain
            or receipt.endpoint != endpoint
        ):
            raise ReceiptBindingError("selection receipt binding mismatch")
        return receipt

    async def claim_formal_failure(self, receipt: SelectionReceipt) -> bool:
        if receipt.selection_source != "formal":
            raise ValueError("receipt is not formal")
        result = await self._redis.eval(
            """
            local raw = redis.call('GET', KEYS[1])
            if not raw then return -1 end
            local value = cjson.decode(raw)
            if value.selection_source ~= 'formal' then return -2 end
            if value.failure_applied then return 0 end
            local ttl = redis.call('PTTL', KEYS[1])
            if ttl <= 0 then return -1 end
            value.failure_applied = true
            redis.call('SET', KEYS[1], cjson.encode(value), 'PX', ttl)
            return 1
            """,
            1,
            self.key(receipt.digest),
        )
        if int(result) < 0:
            raise InvalidReceipt("invalid selection receipt")
        return bool(result)
