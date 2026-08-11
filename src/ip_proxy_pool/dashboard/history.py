"""Two-resolution Redis history for dashboard aggregates."""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from pydantic import ValidationError

from ip_proxy_pool.dashboard.keys import HistoryKeys, history_keys
from ip_proxy_pool.dashboard.lua import WRITE_HISTORY
from ip_proxy_pool.dashboard.models import (
    HistoryPoint,
    HistoryRange,
    HistoryResolution,
    HistorySeries,
    range_policy,
)


class HistoryDataError(ValueError):
    """Raised when persisted dashboard history is invalid."""


class DashboardHistoryStore:
    def __init__(
        self,
        redis: Any,
        *,
        prefix: str,
        short_hours: int,
        long_hours: int,
    ) -> None:
        if not prefix:
            raise ValueError("prefix must be non-empty")
        if short_hours < 1 or long_hours < 1:
            raise ValueError("retention hours must be positive")
        self._redis = redis
        self._prefix = prefix
        self._short_hours = short_hours
        self._long_hours = long_hours

    def keys(self, scope: str, resolution: HistoryResolution) -> HistoryKeys:
        return history_keys(self._prefix, scope, resolution)

    @staticmethod
    def _bucket_time(value: datetime, resolution: HistoryResolution) -> datetime:
        if value.tzinfo is None:
            raise ValueError("history timestamps must be timezone-aware")
        utc = value.astimezone(UTC)
        minute = (utc.minute // 5) * 5 if resolution is HistoryResolution.FIVE_MINUTES else 0
        return utc.replace(minute=minute, second=0, microsecond=0)

    @staticmethod
    def _bucket_name(value: datetime, resolution: HistoryResolution) -> str:
        pattern = "%Y%m%d%H%M" if resolution is HistoryResolution.FIVE_MINUTES else "%Y%m%d%H"
        return value.strftime(pattern)

    async def write(
        self,
        scope: str,
        resolution: HistoryResolution,
        point: HistoryPoint,
    ) -> int:
        observed_at = self._bucket_time(point.observed_at, resolution)
        normalized = point.model_copy(update={"observed_at": observed_at})
        keys = self.keys(scope, resolution)
        retention = (
            self._short_hours if resolution is HistoryResolution.FIVE_MINUTES else self._long_hours
        )
        maximum = 576 if resolution is HistoryResolution.FIVE_MINUTES else 720
        cutoff = (observed_at - timedelta(hours=retention)).timestamp()
        result = await self._redis.eval(
            WRITE_HISTORY,
            2,
            keys.index,
            keys.data,
            self._bucket_name(observed_at, resolution),
            observed_at.timestamp(),
            normalized.model_dump_json(),
            cutoff,
            maximum,
        )
        return int(result)

    async def read(
        self,
        scope: str,
        range_: HistoryRange,
        *,
        now: datetime,
    ) -> HistorySeries:
        if now.tzinfo is None:
            raise ValueError("history timestamps must be timezone-aware")
        policy = range_policy(range_)
        keys = self.keys(scope, policy.resolution)
        end = now.astimezone(UTC)
        start = end - timedelta(hours=policy.hours)
        buckets = cast(
            list[str],
            await self._redis.zrangebyscore(
                keys.index,
                start.timestamp(),
                end.timestamp(),
                start=0,
                num=policy.max_points,
            ),
        )
        if not buckets:
            return HistorySeries(range=range_, resolution=policy.resolution, points=())
        payloads = cast(list[str | None], await self._redis.hmget(keys.data, buckets))
        try:
            points = tuple(
                HistoryPoint.model_validate_json(payload)
                for payload in payloads
                if payload is not None
            )
        except (ValidationError, ValueError, TypeError) as error:
            raise HistoryDataError("invalid dashboard history") from error
        if len(points) != len(buckets):
            raise HistoryDataError("invalid dashboard history")
        return HistorySeries(range=range_, resolution=policy.resolution, points=points)
