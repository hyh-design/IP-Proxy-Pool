from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, cast

from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState
from ip_proxy_pool.security.network import validate_proxy_endpoint


async def iter_legacy_records(
    redis: Any,
    key: str,
    domain: str,
    now: datetime,
) -> AsyncIterator[ProxyRecord]:
    """Read valid legacy ZSET members without mutating the source key."""
    if not key or not domain:
        raise ValueError("key and domain must be non-empty")

    cursor = 0
    while True:
        cursor, raw_items = await redis.zscan(key, cursor=cursor, count=200)
        for member, raw_score in cast(list[tuple[str, float]], raw_items):
            try:
                endpoint = validate_proxy_endpoint(ProxyEndpoint.parse(member))
            except ValueError:
                continue
            score = max(0, min(70, int(raw_score)))
            yield ProxyRecord(
                endpoint=endpoint,
                domain=domain,
                score=score,
                state=ProxyState.CANDIDATE,
                source_names={f"legacy:{key}"},
                first_seen_at=now,
                last_seen_at=now,
                next_check_at=now,
            )
        if cursor == 0:
            break
