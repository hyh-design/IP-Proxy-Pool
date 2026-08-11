import random
from collections.abc import Callable
from datetime import datetime, timedelta

from ip_proxy_pool.models import ProxyRecord, ProxyState


def check_interval_seconds(record: ProxyRecord) -> tuple[int, int]:
    if record.state is ProxyState.CANDIDATE:
        return (30, 60)
    if record.state is ProxyState.QUARANTINED:
        return (21600, 86400)
    if record.score >= 90:
        return (300, 600)
    if record.score >= 70:
        return (120, 300)
    return (30, 90)


def next_check_at(
    record: ProxyRecord,
    *,
    now: datetime,
    jitter: Callable[[int, int], int] = random.randint,
) -> datetime:
    lower, upper = check_interval_seconds(record)
    return now + timedelta(seconds=jitter(lower, upper))
