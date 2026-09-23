from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True, slots=True)
class PoolKeys:
    records: str
    quality: str
    due: str
    leased: str
    lease_owners: str
    available_latency: str
    available_latency_ready: str
    priority_due: str
    priority_due_ready: str


@dataclass(frozen=True, slots=True)
class PeerCacheKeys:
    records: str
    expiry: str
    suppression: str
    suppression_expiry: str
    evictions: str
    generation: str
    lock: str
    state: str
    receipts_prefix: str


def peer_cache_keys_for(prefix: str, domain: str, peer_name: str) -> PeerCacheKeys:
    if not prefix or not domain or not peer_name:
        raise ValueError("prefix, domain and peer_name must be non-empty")
    base = f"{prefix}:peer-cache:{quote(domain, safe='.-_')}:{quote(peer_name, safe='.-_')}"
    return PeerCacheKeys(
        records=f"{base}:records",
        expiry=f"{base}:expiry",
        suppression=f"{base}:suppression",
        suppression_expiry=f"{base}:suppression-expiry",
        evictions=f"{base}:evictions",
        generation=f"{base}:generation",
        lock=f"{base}:lock",
        state=f"{base}:state",
        receipts_prefix=f"{prefix}:peer-cache:receipt:",
    )


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
        available_latency=f"{base}:available-latency",
        available_latency_ready=f"{base}:available-latency-ready",
        priority_due=f"{base}:priority-due",
        priority_due_ready=f"{base}:priority-due-ready",
    )
