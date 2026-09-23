from uuid import uuid4

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
        lead_code="L-42",
        record_id=42,
        entered_public_pool_at="2026-09-23 08:00:00",
    )


async def test_one_found_resolves_internal_across_clients(isolated_redis: str) -> None:
    first = Redis.from_url(isolated_redis, decode_responses=True)
    second = Redis.from_url(isolated_redis, decode_responses=True)
    clock = Clock()
    try:
        left = OwnershipStore(first, prefix="case-test", clock_ms=clock)
        right = OwnershipStore(second, prefix="case-test", clock_ms=clock)
        case = await left.open_case(str(uuid4()), cycle(), ("one:a", "two:b"))
        job = await right.claim_job("two:b")
        assert job is not None
        final = await right.submit_result(
            case.case_id, "two:b", 1, "found", str(uuid4()), job.lease_token
        )
        assert final.status == "internal"
        assert final.revision == 1
        assert (await left.read_case(case.case_id)).status == "internal"
    finally:
        await first.aclose()
        await second.aclose()


async def test_two_complete_negative_rounds_required(isolated_redis: str) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    clock = Clock()
    try:
        store = OwnershipStore(redis, prefix="case-test", clock_ms=clock)
        case = await store.open_case(str(uuid4()), cycle(), ("one:a", "two:b"))
        for member in case.members:
            job = await store.claim_job(member)
            assert job is not None and job.round_no == 1
            snapshot = await store.submit_result(
                case.case_id, member, 1, "absent", str(uuid4()), job.lease_token
            )
            assert snapshot.status == "pending"
        assert await store.claim_job("one:a") is None
        clock.advance(29)
        assert await store.claim_job("one:a") is None
        clock.advance(1)
        first = await store.claim_job("one:a")
        assert first is not None and first.round_no == 2
        await store.submit_result(
            case.case_id, "one:a", 2, "absent", str(uuid4()), first.lease_token
        )
        assert (await store.read_case(case.case_id)).status == "pending"
        second = await store.claim_job("two:b")
        assert second is not None and second.round_no == 2
        final = await store.submit_result(
            case.case_id, "two:b", 2, "absent", str(uuid4()), second.lease_token
        )
        assert final.status == "external"
        assert final.revision == 1
        assert await redis.zcard("case-test:reclaim:ownership:jobs:one:a") == 0
        assert await redis.zcard("case-test:reclaim:ownership:jobs:two:b") == 0
    finally:
        await redis.aclose()


async def test_late_success_corrects_external_once(isolated_redis: str) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    clock = Clock()
    try:
        store = OwnershipStore(redis, prefix="case-test", clock_ms=clock)
        case = await store.open_case(str(uuid4()), cycle(), ("one:a", "two:b"))
        for round_no in (1, 2):
            if round_no == 2:
                clock.advance(30)
            for member in case.members:
                job = await store.claim_job(member)
                assert job is not None and job.round_no == round_no
                await store.submit_result(
                    case.case_id, member, round_no, "absent", str(uuid4()), job.lease_token
                )
        assert (await store.read_case(case.case_id)).status == "external"
        event_id = str(uuid4())
        await store.register_success(cycle(), event_id, "one:a")
        await store.register_success(cycle(), event_id, "one:a")
        corrected = await store.read_case(case.case_id)
        assert corrected.status == "internal"
        assert corrected.revision == 2
    finally:
        await redis.aclose()


async def test_second_round_waits_fifteen_seconds_after_late_first_round(
    isolated_redis: str,
) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    clock = Clock()
    try:
        store = OwnershipStore(redis, prefix="case-test", clock_ms=clock)
        case = await store.open_case(str(uuid4()), cycle(), ("one:a", "two:b"))
        first = await store.claim_job("one:a")
        assert first is not None
        await store.submit_result(
            case.case_id, "one:a", 1, "absent", str(uuid4()), first.lease_token
        )
        clock.advance(20)
        second = await store.claim_job("two:b")
        assert second is not None
        await store.submit_result(
            case.case_id, "two:b", 1, "absent", str(uuid4()), second.lease_token
        )
        clock.advance(14)
        assert await store.claim_job("one:a") is None
        clock.advance(1)
        job = await store.claim_job("one:a")
        assert job is not None and job.round_no == 2
    finally:
        await redis.aclose()


async def test_offline_member_is_never_implicitly_absent(isolated_redis: str) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    clock = Clock()
    try:
        store = OwnershipStore(redis, prefix="case-test", clock_ms=clock)
        case = await store.open_case(str(uuid4()), cycle(), ("one:a", "two:b"))
        job = await store.claim_job("one:a")
        assert job is not None
        await store.submit_result(case.case_id, "one:a", 1, "absent", str(uuid4()), job.lease_token)
        clock.advance(24 * 3600)
        assert await store.reap_expired_cases() == 1
        snapshot = await store.read_case(case.case_id)
        assert snapshot.status == "unconfirmed"
        assert snapshot.revision == 1
        assert await redis.zcard("case-test:reclaim:ownership:jobs:two:b") == 0
    finally:
        await redis.aclose()


async def test_existing_success_shortcuts_only_matching_cycle(isolated_redis: str) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    try:
        store = OwnershipStore(redis, prefix="case-test")
        await store.register_success(cycle(), str(uuid4()), "one:a")
        matching = await store.open_case(str(uuid4()), cycle(), ("one:a", "two:b"))
        assert matching.status == "internal"
        later = cycle().model_copy(update={"entered_public_pool_at": "2026-09-23 09:00:00"})
        pending = await store.open_case(str(uuid4()), later, ("one:a", "two:b"))
        assert pending.status == "pending"
    finally:
        await redis.aclose()
