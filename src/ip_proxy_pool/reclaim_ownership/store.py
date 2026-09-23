"""Redis-backed, atomic cycle-bound success receipts."""

import hashlib
from typing import Any, cast

from ip_proxy_pool.reclaim_ownership.models import LeadCycle, SuccessReceipt

SUCCESS_TTL_SECONDS = 48 * 60 * 60


class SuccessConflict(ValueError):
    """A different member or event claimed an already registered cycle."""


_REGISTER_SUCCESS = """
local existing_event = redis.call('HGETALL', KEYS[2])
if #existing_event > 0 then
  if redis.call('HGET', KEYS[2], 'identity') ~= ARGV[3] then
    return {0}
  end
  local original_key = redis.call('HGET', KEYS[2], 'receipt_key')
  if not original_key or redis.call('EXISTS', original_key) == 0 then
    return {0}
  end
  return {1, redis.call('HGET', original_key, 'member_id'),
    redis.call('HGET', original_key, 'event_id'),
    redis.call('HGET', original_key, 'server_time'),
    redis.call('HGET', original_key, 'expires_at')}
end
if redis.call('EXISTS', KEYS[1]) == 1 then
  return {0}
end
local now = redis.call('TIME')
local now_ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
local expires_at = now_ms + tonumber(ARGV[4]) * 1000
redis.call('HSET', KEYS[1], 'member_id', ARGV[1], 'event_id', ARGV[2],
  'server_time', now_ms, 'expires_at', expires_at)
redis.call('EXPIRE', KEYS[1], ARGV[4])
redis.call('HSET', KEYS[2], 'identity', ARGV[3], 'receipt_key', KEYS[1])
redis.call('EXPIRE', KEYS[2], ARGV[4])
return {1, ARGV[1], ARGV[2], now_ms, expires_at}
"""


def _hash_fields(fields: tuple[str, ...]) -> str:
    payload = b"".join(len(value.encode()).to_bytes(4, "big") + value.encode() for value in fields)
    return hashlib.sha256(payload).hexdigest()


def cycle_digest(cycle: LeadCycle) -> str | None:
    entry_time = cycle.reliable_entry_time()
    if entry_time is None:
        return None
    return _hash_fields((cycle.domain, cycle.lead_code, str(cycle.record_id), entry_time))


class OwnershipStore:
    def __init__(self, redis: Any, *, prefix: str) -> None:
        self._redis = redis
        self._base = f"{prefix}:reclaim:ownership"

    async def register_success(
        self, cycle: LeadCycle, event_id: str, member_id: str
    ) -> SuccessReceipt:
        if not event_id or not member_id:
            raise ValueError("event ID and member ID are required")
        digest = cycle_digest(cycle)
        identity_time = cycle.reliable_entry_time() or cycle.entered_public_pool_at or ""
        identity = _hash_fields(
            (cycle.domain, cycle.lead_code, str(cycle.record_id), identity_time)
        )
        receipt_key = f"{self._base}:success:{digest or 'unknown-' + event_id}"
        event_key = f"{self._base}:event:{event_id}"
        values = cast(
            list[str | int],
            await self._redis.eval(
                _REGISTER_SUCCESS,
                2,
                receipt_key,
                event_key,
                member_id,
                event_id,
                f"{identity}:{member_id}",
                SUCCESS_TTL_SECONDS,
            ),
        )
        if int(values[0]) != 1:
            raise SuccessConflict("conflicting success receipt")
        return SuccessReceipt(
            member_id=str(values[1]),
            event_id=str(values[2]),
            server_time=int(values[3]),
            expires_at=int(values[4]),
        )

    async def find_success(self, cycle: LeadCycle) -> SuccessReceipt | None:
        digest = cycle_digest(cycle)
        if digest is None:
            return None
        data = cast(dict[str, str], await self._redis.hgetall(f"{self._base}:success:{digest}"))
        if not data:
            return None
        return SuccessReceipt(
            member_id=data["member_id"],
            event_id=data["event_id"],
            server_time=int(data["server_time"]),
            expires_at=int(data["expires_at"]),
        )
