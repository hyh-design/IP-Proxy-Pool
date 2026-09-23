from uuid import uuid4

import pytest
from pydantic import ValidationError
from redis.asyncio import Redis

from ip_proxy_pool.reclaim_ownership.models import LeadCycle
from ip_proxy_pool.reclaim_ownership.store import OwnershipStore, SuccessConflict


def cycle(
    *, code: str = "L-42", record_id: int = 42, entered: str | None = "2026-09-23 08:00:00"
) -> LeadCycle:
    return LeadCycle(
        domain="portal.daqihui.com",
        lead_code=code,
        record_id=record_id,
        entered_public_pool_at=entered,
    )


async def test_success_is_idempotent_and_retained_without_quota_keys(isolated_redis: str) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    try:
        store = OwnershipStore(redis, prefix="ownership-test")
        event_id = str(uuid4())
        first = await store.register_success(cycle(), event_id, "one:a")
        second = await store.register_success(cycle(), event_id, "one:a")

        assert first == second
        assert first.member_id == "one:a"
        assert first.event_id == event_id
        assert first.expires_at > first.server_time
        receipt_keys = await redis.keys("ownership-test:reclaim:ownership:success:*")
        assert len(receipt_keys) == 1
        assert 172700 <= await redis.ttl(receipt_keys[0]) <= 172800
        assert await redis.keys("ownership-test:reclaim:global-quota:*") == []
    finally:
        await redis.aclose()


async def test_shortcut_is_cycle_bound_and_ignores_unreliable_time(isolated_redis: str) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    try:
        store = OwnershipStore(redis, prefix="ownership-test")
        old = cycle()
        await store.register_success(old, str(uuid4()), "one:a")

        assert (await store.find_success(old)).member_id == "one:a"
        normalized = old.model_copy(update={"entered_public_pool_at": "2026-09-23T08:00:00"})
        assert (await store.find_success(normalized)).member_id == "one:a"
        assert await store.find_success(cycle(entered="2026-09-23 09:00:00")) is None
        assert await store.find_success(cycle(record_id=43)) is None
        assert await store.find_success(cycle(entered=None)) is None
        assert await store.find_success(cycle(entered="-")) is None
    finally:
        await redis.aclose()


async def test_retry_same_event_accepts_equivalent_time_format(isolated_redis: str) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    try:
        store = OwnershipStore(redis, prefix="ownership-test")
        event_id = str(uuid4())
        first = await store.register_success(cycle(), event_id, "one:a")
        second = await store.register_success(
            cycle(entered="2026-09-23T08:00:00"), event_id, "one:a"
        )
        assert second == first
    finally:
        await redis.aclose()


async def test_other_member_success_on_same_cycle_is_conflict(isolated_redis: str) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    try:
        store = OwnershipStore(redis, prefix="ownership-test")
        await store.register_success(cycle(), str(uuid4()), "one:a")
        with pytest.raises(SuccessConflict):
            await store.register_success(cycle(), str(uuid4()), "two:b")
        assert (await store.find_success(cycle())).member_id == "one:a"
    finally:
        await redis.aclose()


def test_lead_cycle_rejects_non_portal_domain_bad_code_and_record_id() -> None:
    for values in (
        {"domain": "example.com"},
        {"lead_code": ""},
        {"lead_code": "x" * 257},
        {"record_id": 0},
    ):
        with pytest.raises(ValidationError):
            LeadCycle.model_validate({**cycle().model_dump(), **values})
