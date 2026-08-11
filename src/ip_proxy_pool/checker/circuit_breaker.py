from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast
from urllib.parse import quote

from ip_proxy_pool.observability.metrics import Metrics, NoopMetrics


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class CircuitDecision:
    allowed: bool
    state: CircuitState


_BEFORE_PROBE = """
local state = redis.call('HGET', KEYS[1], 'state') or 'closed'
if state == 'closed' then
  return {1, 'closed'}
end
if state == 'open' then
  local opened_at = tonumber(redis.call('HGET', KEYS[1], 'opened_at') or '0')
  if tonumber(ARGV[1]) - opened_at >= tonumber(ARGV[2]) then
    redis.call('HSET', KEYS[1], 'state', 'half_open', 'half_open_owner', ARGV[3])
    return {1, 'half_open'}
  end
  return {0, 'open'}
end
if state == 'half_open' then
  local owner = redis.call('HGET', KEYS[1], 'half_open_owner')
  if owner == ARGV[3] then
    return {1, 'half_open'}
  end
  return {0, 'half_open'}
end
return {0, state}
"""

_RECORD_FAILURE = """
local state = redis.call('HGET', KEYS[1], 'state') or 'closed'
local count = tonumber(redis.call('HGET', KEYS[1], 'failure_count') or '0') + 1
if state == 'half_open' then
  redis.call('HSET', KEYS[1], 'state', 'open', 'failure_count', count, 'opened_at', ARGV[1])
  redis.call('HDEL', KEYS[1], 'half_open_owner')
  return 'open'
end
if state == 'open' then
  redis.call('HSET', KEYS[1], 'failure_count', count)
  return 'open'
end
if count >= tonumber(ARGV[2]) then
  redis.call('HSET', KEYS[1], 'state', 'open', 'failure_count', count, 'opened_at', ARGV[1])
  return 'open'
end
redis.call('HSET', KEYS[1], 'state', 'closed', 'failure_count', count)
return 'closed'
"""

_RECORD_SUCCESS = """
redis.call('HSET', KEYS[1], 'state', 'closed', 'failure_count', 0)
redis.call('HDEL', KEYS[1], 'opened_at', 'half_open_owner')
return 'closed'
"""


class RedisCircuitBreaker:
    def __init__(
        self,
        redis: Any,
        *,
        prefix: str,
        failures: int,
        cooldown: int,
        metrics: Metrics | None = None,
    ) -> None:
        if not prefix:
            raise ValueError("prefix must be non-empty")
        if failures < 1 or cooldown < 1:
            raise ValueError("failures and cooldown must be positive")
        self._redis = redis
        self._prefix = prefix
        self._failures = failures
        self._cooldown = cooldown
        self._metrics = metrics or NoopMetrics()

    def _key(self, target_name: str) -> str:
        if not target_name:
            raise ValueError("target_name must be non-empty")
        return f"{self._prefix}:breaker:{quote(target_name, safe='.-_')}"

    async def before_probe(
        self,
        target_name: str,
        *,
        owner: str,
        now: float,
    ) -> CircuitDecision:
        if not owner:
            raise ValueError("owner must be non-empty")
        result = cast(
            list[str | int],
            await self._redis.eval(
                _BEFORE_PROBE,
                1,
                self._key(target_name),
                str(now),
                str(self._cooldown),
                owner,
            ),
        )
        raw_state = result[1]
        if isinstance(raw_state, bytes):
            raw_state = raw_state.decode()
        return CircuitDecision(bool(int(result[0])), CircuitState(str(raw_state)))

    async def record_success(self, target_name: str) -> None:
        await self._redis.eval(_RECORD_SUCCESS, 1, self._key(target_name))
        self._metrics.circuit_transition(target_name, CircuitState.CLOSED)

    async def record_failure(self, target_name: str, *, now: float) -> None:
        state = await self._redis.eval(
            _RECORD_FAILURE,
            1,
            self._key(target_name),
            str(now),
            str(self._failures),
        )
        if isinstance(state, bytes):
            state = state.decode()
        self._metrics.circuit_transition(target_name, str(state))
