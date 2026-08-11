from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True, slots=True)
class PoolKeys:
    records: str
    quality: str
    due: str
    leased: str
    lease_owners: str


def keys_for(prefix: str, domain: str) -> PoolKeys:
    """Build the stable Redis key set for one target domain."""
    if not prefix or not domain:
        raise ValueError("prefix and domain must be non-empty")

    base = f"{prefix}:pool:{quote(domain, safe='.-_')}"
    return PoolKeys(
        records=f"{base}:records",
        quality=f"{base}:quality",
        due=f"{base}:due",
        leased=f"{base}:leased",
        lease_owners=f"{base}:lease-owners",
    )
