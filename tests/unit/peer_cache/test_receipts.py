from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis

from ip_proxy_pool.peer_cache.models import PeerExportItem
from ip_proxy_pool.peer_cache.receipts import (
    InvalidReceipt,
    ReceiptBindingError,
    SelectionReceiptStore,
)


async def test_formal_receipt_is_opaque_bound_and_survives_store_restart(
    isolated_redis: str,
) -> None:
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    try:
        now = datetime.now(UTC)
        snapshot = PeerExportItem(
            endpoint="1.1.1.1:80",
            domain="portal.daqihui.com",
            score=95,
            latency_ewma_ms=100,
            last_checked_at=now,
            consecutive_successes=3,
            source_names=("formal",),
        )
        first = SelectionReceiptStore(
            redis, prefix="test", domain=snapshot.domain, peer_name="system-two"
        )
        token = await first.issue(snapshot, "fingerprint-a", now)
        second = SelectionReceiptStore(
            redis, prefix="test", domain=snapshot.domain, peer_name="system-two"
        )
        receipt = await second.resolve(
            token, "fingerprint-a", snapshot.domain, snapshot.endpoint, now
        )
        assert receipt.selection_source == "formal"
        assert token not in (await redis.get(second.key(receipt.digest)))
        for fingerprint, domain, endpoint in (
            ("fingerprint-b", snapshot.domain, snapshot.endpoint),
            ("fingerprint-a", "wrong.example", snapshot.endpoint),
            ("fingerprint-a", snapshot.domain, "8.8.8.8:80"),
        ):
            with pytest.raises(ReceiptBindingError):
                await second.resolve(token, fingerprint, domain, endpoint, now)
        another_domain = SelectionReceiptStore(
            redis, prefix="test", domain="wrong.example", peer_name="different-peer"
        )
        with pytest.raises(ReceiptBindingError):
            await another_domain.resolve(
                token, "fingerprint-a", "wrong.example", snapshot.endpoint, now
            )
        with pytest.raises(InvalidReceipt):
            await second.resolve("wrong", "fingerprint-a", snapshot.domain, snapshot.endpoint, now)
        with pytest.raises(InvalidReceipt):
            await second.resolve(
                token,
                "fingerprint-a",
                snapshot.domain,
                snapshot.endpoint,
                now + timedelta(seconds=600),
            )
    finally:
        await redis.aclose()
