import random
from collections.abc import Callable
from datetime import datetime, timedelta

from ip_proxy_pool.models import ProxyRecord, ProxyState


def check_interval_seconds(record: ProxyRecord) -> tuple[int, int]:
    if record.state is ProxyState.QUARANTINED:
        if record.consecutive_failures <= 1:
            return (300, 600)
        if record.consecutive_failures == 2:
            return (900, 1800)
        if record.consecutive_failures <= 4:
            return (3600, 7200)
        return (21600, 86400)
    if record.consecutive_failures >= 3:
        return (3600, 7200)
    if record.consecutive_failures == 2:
        return (900, 1800)
    if record.consecutive_failures == 1:
        return (300, 600)
    if record.state is ProxyState.CANDIDATE:
        return (30, 60)
    if record.score >= 90:
        return (180, 300)
    if record.state is ProxyState.AVAILABLE and record.score >= 80:
        return (180, 300)
    if record.score >= 70:
        return (900, 1800)
    return (1800, 3600)


def next_check_at(
    record: ProxyRecord,
    *,
    now: datetime,
    jitter: Callable[[int, int], int] = random.randint,
) -> datetime:
    lower, upper = check_interval_seconds(record)
    return now + timedelta(seconds=jitter(lower, upper))
