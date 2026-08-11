"""Bounded Redis-backed collector and checker heartbeats."""

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

_INSTANCE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_ROLES = frozenset({"collector", "checker"})

_BEAT = """
redis.call('SET', KEYS[2], ARGV[1], 'EX', ARGV[3])
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[2])
local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[4])
for _, instance_id in ipairs(expired) do
  redis.call('DEL', ARGV[5] .. instance_id)
  redis.call('ZREM', KEYS[1], instance_id)
end
local overflow = redis.call('ZCARD', KEYS[1]) - 100
if overflow > 0 then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, overflow - 1)
  for _, instance_id in ipairs(oldest) do
    redis.call('DEL', ARGV[5] .. instance_id)
    redis.call('ZREM', KEYS[1], instance_id)
  end
end
return redis.call('ZCARD', KEYS[1])
"""


class RoleStatus(StrEnum):
    HEALTHY = "healthy"
    STALE = "stale"
    DOWN = "down"


class RoleHealth(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: str
    status: RoleStatus
    active_instances: int = Field(ge=0, le=100)
    newest_heartbeat_at: datetime | None = None


class WorkerHeartbeatStore:
    def __init__(
        self,
        redis: Any,
        *,
        prefix: str,
        interval_seconds: int,
        ttl_seconds: int,
    ) -> None:
        if not prefix:
            raise ValueError("prefix must be non-empty")
        if interval_seconds < 1 or ttl_seconds < interval_seconds * 2:
            raise ValueError("heartbeat TTL must be at least two intervals")
        self._redis = redis
        self._prefix = prefix
        self._interval_seconds = interval_seconds
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def _validate_role(role: str) -> None:
        if role not in _ROLES:
            raise ValueError("worker role must be collector or checker")

    @staticmethod
    def _validate_now(now: datetime) -> datetime:
        if now.tzinfo is None:
            raise ValueError("heartbeat time must be timezone-aware")
        return now.astimezone(UTC)

    def _index_key(self, role: str) -> str:
        return f"{self._prefix}:worker-heartbeat:index:{role}"

    def _instance_prefix(self, role: str) -> str:
        return f"{self._prefix}:worker-heartbeat:{role}:"

    async def beat(self, role: str, instance_id: str, *, now: datetime) -> int:
        self._validate_role(role)
        if _INSTANCE_ID.fullmatch(instance_id) is None:
            raise ValueError("instance ID must contain only safe characters")
        utc = self._validate_now(now)
        prefix = self._instance_prefix(role)
        result = await self._redis.eval(
            _BEAT,
            2,
            self._index_key(role),
            f"{prefix}{instance_id}",
            utc.timestamp(),
            instance_id,
            self._ttl_seconds,
            utc.timestamp() - self._ttl_seconds,
            prefix,
        )
        return int(result)

    async def health(self, role: str, *, now: datetime) -> RoleHealth:
        self._validate_role(role)
        utc = self._validate_now(now)
        entries = cast(
            list[tuple[str, float]],
            await self._redis.zrevrange(self._index_key(role), 0, 99, withscores=True),
        )
        if not entries:
            return RoleHealth(role=role, status=RoleStatus.DOWN, active_instances=0)

        pipeline = self._redis.pipeline(transaction=False)
        prefix = self._instance_prefix(role)
        for instance_id, _ in entries:
            pipeline.exists(f"{prefix}{instance_id}")
        exists = cast(list[int], await pipeline.execute())

        live: list[tuple[str, float]] = []
        dead: list[str] = []
        for (instance_id, timestamp), present in zip(entries, exists, strict=True):
            age = utc.timestamp() - float(timestamp)
            if present and 0 <= age <= self._ttl_seconds:
                live.append((instance_id, float(timestamp)))
            else:
                dead.append(instance_id)
        if dead:
            await self._redis.zrem(self._index_key(role), *dead)
        if not live:
            return RoleHealth(role=role, status=RoleStatus.DOWN, active_instances=0)

        newest_timestamp = max(timestamp for _, timestamp in live)
        newest_age = utc.timestamp() - newest_timestamp
        status = (
            RoleStatus.HEALTHY if newest_age <= self._interval_seconds * 2 else RoleStatus.STALE
        )
        return RoleHealth(
            role=role,
            status=status,
            active_instances=len(live),
            newest_heartbeat_at=datetime.fromtimestamp(newest_timestamp, tz=UTC),
        )
