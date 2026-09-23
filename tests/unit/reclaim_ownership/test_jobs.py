from uuid import uuid4

import pytest
from redis.asyncio import Redis

from ip_proxy_pool.reclaim_ownership.models import LeadCycle
from ip_proxy_pool.reclaim_ownership.store import OwnershipStore


class Clock:
    def __init__(self) -> None:
        self.value = 1_800_000_000_000

    def __call__(self) -> int:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += seconds * 1000


def cycle() -> LeadCycle:
    return LeadCycle(
        domain="portal.daqihui.com",
        lead_code="L-43",
        record_id=43,
        entered_public_pool_at="2026-09-23 08:00:00",
    )


async def test_lease_fences_stale_result_and_reclaims_after_expiry(isolated_redis: str) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    clock = Clock()
    try:
        store = OwnershipStore(redis, prefix="job-test", clock_ms=clock)
        case = await store.open_case(str(uuid4()), cycle(), ("one:a", "two:b"))
        job = await store.claim_job("one:a")
        assert job is not None
        assert await store.claim_job("one:a") is None
        clock.advance(31)
        replacement = await store.claim_job("one:a")
        assert replacement is not None
        assert replacement.lease_token != job.lease_token
        with pytest.raises(ValueError, match="stale"):
            await store.submit_result(
                case.case_id, "one:a", 1, "absent", str(uuid4()), job.lease_token
            )
        result_id = str(uuid4())
        first = await store.submit_result(
            case.case_id, "one:a", 1, "absent", result_id, replacement.lease_token
        )
        clock.advance(31)
        replay = await store.submit_result(
            case.case_id, "one:a", 1, "absent", result_id, replacement.lease_token
        )
        assert replay == first
    finally:
        await redis.aclose()


async def test_error_requeues_without_absence_and_rejects_nonmember(isolated_redis: str) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    clock = Clock()
    try:
        store = OwnershipStore(redis, prefix="job-test", clock_ms=clock)
        case = await store.open_case(str(uuid4()), cycle(), ("one:a", "two:b"))
        assert await store.claim_job("unknown") is None
        job = await store.claim_job("one:a")
        assert job is not None
        with pytest.raises(ValueError, match="member"):
            await store.submit_result(
                case.case_id, "unknown", 1, "absent", str(uuid4()), job.lease_token
            )
        pending = await store.submit_result(
            case.case_id, "one:a", 1, "error", str(uuid4()), job.lease_token
        )
        assert pending.status == "pending"
        assert await store.claim_job("one:a") is None
        clock.advance(30)
        retry = await store.claim_job("one:a")
        assert retry is not None and retry.round_no == 1
    finally:
        await redis.aclose()
