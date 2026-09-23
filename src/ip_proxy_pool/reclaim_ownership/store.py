"""Redis-backed, atomic cycle-bound success receipts."""

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from redis.exceptions import ResponseError

from ip_proxy_pool.reclaim_ownership.lua import CLAIM_JOB, OPEN_CASE, REAP_EXPIRED, SUBMIT_RESULT
from ip_proxy_pool.reclaim_ownership.models import CaseSnapshot, CheckJob, LeadCycle, SuccessReceipt

SUCCESS_TTL_SECONDS = 48 * 60 * 60


class SuccessConflict(ValueError):
    """A different member or event claimed an already registered cycle."""


_REGISTER_SUCCESS = """
local existing_event = redis.call('EXISTS', KEYS[2])
local receipt_key = KEYS[1]
if existing_event == 1 then
  if redis.call('HGET', KEYS[2], 'identity') ~= ARGV[3] then
    return {0}
  end
  receipt_key = redis.call('HGET', KEYS[2], 'receipt_key')
  if not receipt_key or redis.call('EXISTS', receipt_key) == 0 then
    return {0}
  end
else
  if redis.call('EXISTS', KEYS[1]) == 1 then return {0} end
  local now
  if tonumber(ARGV[5]) and tonumber(ARGV[5]) >= 0 then
    now = tonumber(ARGV[5])
  else
    local clock = redis.call('TIME')
    now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
  end
  local expires_at = now + tonumber(ARGV[4]) * 1000
  redis.call('HSET', KEYS[1], 'member_id', ARGV[1], 'event_id', ARGV[2],
    'server_time', now, 'expires_at', expires_at)
  redis.call('EXPIRE', KEYS[1], ARGV[4])
  redis.call('HSET', KEYS[2], 'identity', ARGV[3], 'receipt_key', KEYS[1])
  redis.call('EXPIRE', KEYS[2], ARGV[4])
end
local received = tonumber(redis.call('HGET', receipt_key, 'server_time')) or 0
if tonumber(ARGV[6]) > 0 and received >= tonumber(ARGV[6]) then
  local cases = redis.call('SMEMBERS', KEYS[3])
  for _, case_id in ipairs(cases) do
    local case_key = ARGV[7] .. ':case:' .. case_id
    local created = tonumber(redis.call('HGET', case_key, 'created_at')) or 0
    local status = redis.call('HGET', case_key, 'status')
    if created >= tonumber(ARGV[6]) and status and status ~= 'internal' then
      redis.call('HSET', case_key, 'status', 'internal')
      redis.call('HINCRBY', case_key, 'revision', 1)
      redis.call('ZREM', KEYS[4], case_id)
      local members = cjson.decode(redis.call('HGET', case_key, 'members_json'))
      for _, member in ipairs(members) do
        redis.call('ZREM', ARGV[7] .. ':jobs:' .. member, case_id .. ':1', case_id .. ':2')
      end
    end
  end
end
return {1, redis.call('HGET', receipt_key, 'member_id'),
  redis.call('HGET', receipt_key, 'event_id'),
  redis.call('HGET', receipt_key, 'server_time'),
  redis.call('HGET', receipt_key, 'expires_at')}
"""


def _hash_fields(fields: tuple[str, ...]) -> str:
    payload = b"".join(len(value.encode()).to_bytes(4, "big") + value.encode() for value in fields)
    return hashlib.sha256(payload).hexdigest()


def cycle_digest(cycle: LeadCycle) -> str | None:
    entry_time = cycle.reliable_entry_time()
    if entry_time is None:
        return None
    return _hash_fields((cycle.domain, cycle.lead_code, str(cycle.record_id), entry_time))


def cycle_entry_ms(cycle: LeadCycle) -> int | None:
    entry = cycle.reliable_entry_time()
    if entry is None:
        return None
    value = datetime.fromisoformat(entry)
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return int(value.timestamp() * 1000)


class OwnershipStore:
    def __init__(
        self, redis: Any, *, prefix: str, clock_ms: Callable[[], int] | None = None
    ) -> None:
        self._redis = redis
        self._base = f"{prefix}:reclaim:ownership"
        self._clock_ms = clock_ms

    def _now_override(self) -> int:
        return self._clock_ms() if self._clock_ms is not None else -1

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
                4,
                receipt_key,
                event_key,
                f"{self._base}:cycle:{digest or 'unreliable'}:cases",
                f"{self._base}:case-deadlines",
                member_id,
                event_id,
                f"{identity}:{member_id}",
                SUCCESS_TTL_SECONDS,
                self._now_override(),
                cycle_entry_ms(cycle) or -1,
                self._base,
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

    async def open_case(
        self,
        case_id: str,
        cycle: LeadCycle,
        members: tuple[str, ...],
        creator_member: str | None = None,
    ) -> CaseSnapshot:
        if not case_id or not members or len(set(members)) != len(members):
            raise ValueError("case ID and unique members are required")
        if creator_member is not None and creator_member not in members:
            raise ValueError("creator must be a case member")
        digest = cycle_digest(cycle)
        case_key = f"{self._base}:case:{case_id}"
        try:
            await self._redis.eval(
                OPEN_CASE,
                4,
                case_key,
                f"{self._base}:success:{digest or 'unreliable'}",
                f"{self._base}:cycle:{digest or 'unreliable'}:cases",
                f"{self._base}:case-deadlines",
                case_id,
                cycle.model_dump_json(),
                digest or "",
                json.dumps(members, separators=(",", ":")),
                creator_member or "",
                self._now_override(),
                self._base,
                SUCCESS_TTL_SECONDS,
                cycle_entry_ms(cycle) or -1,
            )
        except ResponseError as error:
            raise ValueError(str(error)) from error
        return await self.read_case(case_id)

    async def read_case(self, case_id: str) -> CaseSnapshot:
        data = cast(dict[str, str], await self._redis.hgetall(f"{self._base}:case:{case_id}"))
        if not data:
            raise LookupError("unknown ownership case")
        return CaseSnapshot(
            case_id=case_id,
            cycle=LeadCycle.model_validate_json(data["cycle_json"]),
            members=tuple(json.loads(data["members_json"])),
            creator_member=data.get("creator_member") or None,
            status=cast(Any, data["status"]),
            revision=int(data["revision"]),
            created_at=int(data["created_at"]),
        )

    async def claim_job(self, member_id: str) -> CheckJob | None:
        token = str(uuid4())
        values = cast(
            list[str],
            await self._redis.eval(
                CLAIM_JOB,
                1,
                f"{self._base}:jobs:{member_id}",
                member_id,
                self._now_override(),
                self._base,
                token,
            ),
        )
        if not values:
            return None
        return CheckJob(
            case_id=values[0],
            round_no=cast(Any, int(values[1])),
            cycle=LeadCycle.model_validate_json(values[2]),
            lease_token=token,
        )

    async def submit_result(
        self,
        case_id: str,
        member_id: str,
        round_no: int,
        result: str,
        result_id: str,
        lease_token: str,
    ) -> CaseSnapshot:
        try:
            await self._redis.eval(
                SUBMIT_RESULT,
                2,
                f"{self._base}:case:{case_id}",
                f"{self._base}:case-deadlines",
                case_id,
                member_id,
                round_no,
                result,
                result_id,
                self._base,
                self._now_override(),
                lease_token,
            )
        except ResponseError as error:
            raise ValueError(str(error)) from error
        return await self.read_case(case_id)

    async def reap_expired_cases(self) -> int:
        return int(
            await self._redis.eval(
                REAP_EXPIRED,
                1,
                f"{self._base}:case-deadlines",
                self._base,
                self._now_override(),
            )
        )
