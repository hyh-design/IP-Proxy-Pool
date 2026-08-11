"""Stable Redis keys for dashboard data."""

from dataclasses import dataclass
from urllib.parse import quote

from ip_proxy_pool.dashboard.models import HistoryResolution


@dataclass(frozen=True, slots=True)
class HistoryKeys:
    index: str
    data: str


def history_keys(prefix: str, scope: str, resolution: HistoryResolution) -> HistoryKeys:
    if not prefix or not scope:
        raise ValueError("prefix and scope must be non-empty")
    encoded = quote(scope, safe=".-_")
    base = f"{prefix}:dashboard:history:{resolution.value}:{encoded}"
    return HistoryKeys(index=f"{base}:index", data=f"{base}:data")
