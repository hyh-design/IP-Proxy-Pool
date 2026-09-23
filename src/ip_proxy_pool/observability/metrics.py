from collections.abc import Mapping
from typing import Any, Protocol

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram

from ip_proxy_pool.peer_cache.monitor import PeerMetricsSnapshot

_FORBIDDEN_LABELS = {
    "endpoint",
    "url",
    "query",
    "error_message",
    "api_key",
    "authorization",
    "token",
}


def validate_metric_labels(labels: Mapping[str, str]) -> None:
    forbidden = _FORBIDDEN_LABELS.intersection(label.lower() for label in labels)
    if forbidden:
        raise ValueError(f"forbidden label: {sorted(forbidden)[0]}")


class Metrics(Protocol):
    def source_fetch(self, source: str, outcome: str) -> None: ...
    def source_parsed(self, source: str, count: int) -> None: ...
    def record_probe(self, category: str) -> None: ...
    def circuit_transition(self, target: str, state: str) -> None: ...
    def pool_state(self, domain: str, state: str, count: int) -> None: ...
    def tasks(self, domain: str, due: int, leased: int) -> None: ...
    def lease_reclaimed(self, domain: str, count: int) -> None: ...
    def api_request(self, route: str, method: str, status: str, duration: float) -> None: ...
    def api_limit_rejected(self, scope: str) -> None: ...
    def redis_operation(self, operation: str, duration: float, *, error: bool) -> None: ...
    def heartbeat(self, role: str) -> None: ...
    def peer_snapshot(self, domain: str, peer: str, snapshot: PeerMetricsSnapshot) -> None: ...
    def proxy_selection(
        self,
        domain: str,
        *,
        index_members: int,
        candidates: int,
        requested: int,
        returned: int,
        skipped: Mapping[str, int],
        duration: float,
    ) -> None: ...


class PrometheusMetrics:
    def __init__(self, *, registry: CollectorRegistry = REGISTRY) -> None:
        self._source_fetch = Counter(
            "ip_pool_source_fetch_total", "Source fetches", ("source", "outcome"), registry=registry
        )
        self._source_parsed = Counter(
            "ip_pool_source_parsed_total", "Parsed endpoints", ("source",), registry=registry
        )
        self._probes = Counter(
            "ip_pool_probe_total", "Proxy probe outcomes", ("category",), registry=registry
        )
        self._circuits = Counter(
            "ip_pool_circuit_transition_total",
            "Circuit transitions",
            ("target", "state"),
            registry=registry,
        )
        self._pool = Gauge(
            "ip_pool_state", "Proxy pool state size", ("domain", "state"), registry=registry
        )
        self._tasks = Gauge(
            "ip_pool_tasks", "Due and leased tasks", ("domain", "kind"), registry=registry
        )
        self._reclaimed = Counter(
            "ip_pool_lease_reclaimed_total", "Reclaimed leases", ("domain",), registry=registry
        )
        self._api = Histogram(
            "ip_pool_api_request_duration_seconds",
            "API duration",
            ("route", "method", "status"),
            registry=registry,
        )
        self._api_rejected = Counter(
            "ip_pool_api_limit_rejected_total", "API limit rejects", ("scope",), registry=registry
        )
        self._redis_duration = Histogram(
            "ip_pool_redis_duration_seconds", "Redis duration", ("operation",), registry=registry
        )
        self._redis_errors = Counter(
            "ip_pool_redis_error_total", "Redis errors", ("operation",), registry=registry
        )
        self._heartbeat = Gauge(
            "ip_pool_worker_heartbeat_timestamp_seconds",
            "Worker heartbeat",
            ("role",),
            registry=registry,
        )
        self._peer_available = Gauge(
            "ip_pool_peer_metrics_available",
            "Peer Redis metrics read succeeded",
            ("domain", "peer"),
            registry=registry,
        )
        self._peer_valid = Gauge(
            "ip_pool_peer_valid_candidates",
            "Currently valid peer candidates",
            ("domain", "peer"),
            registry=registry,
        )
        self._peer_failures = Gauge(
            "ip_pool_peer_consecutive_sync_failures",
            "Consecutive peer sync failures",
            ("domain", "peer"),
            registry=registry,
        )
        self._peer_heartbeat = Gauge(
            "ip_pool_peer_sync_heartbeat_timestamp_seconds",
            "Peer sync heartbeat",
            ("domain", "peer"),
            registry=registry,
        )
        self._peer_success = Gauge(
            "ip_pool_peer_last_success_timestamp_seconds",
            "Last successful peer sync",
            ("domain", "peer"),
            registry=registry,
        )
        self._peer_evictions = Gauge(
            "ip_pool_peer_evictions_10m",
            "Distinct peer failure receipts in 10 minutes",
            ("domain", "peer"),
            registry=registry,
        )
        self._latency_index_members = Gauge(
            "ip_pool_latency_index_members",
            "Available proxies stored in the latency index",
            ("domain",),
            registry=registry,
        )
        self._selectable_candidates = Gauge(
            "ip_pool_selectable_candidates",
            "Latency-bounded candidates seen by selection",
            ("domain",),
            registry=registry,
        )
        self._selection_total = Counter(
            "ip_pool_proxy_selection_total",
            "Proxy selections by outcome",
            ("domain", "outcome"),
            registry=registry,
        )
        self._selection_returned = Histogram(
            "ip_pool_proxy_selection_returned",
            "Number of proxies returned by one selection",
            ("domain",),
            buckets=(0, 1, 5, 10, 20),
            registry=registry,
        )
        self._selection_skipped = Counter(
            "ip_pool_proxy_selection_skipped_total",
            "Selection candidates skipped by bounded reason",
            ("domain", "reason"),
            registry=registry,
        )
        self._selection_duration = Histogram(
            "ip_pool_proxy_selection_duration_seconds",
            "Proxy selection duration",
            ("domain",),
            registry=registry,
        )

    def source_fetch(self, source: str, outcome: str) -> None:
        self._source_fetch.labels(source=source, outcome=outcome).inc()

    def source_parsed(self, source: str, count: int) -> None:
        self._source_parsed.labels(source=source).inc(count)

    def record_probe(self, category: str) -> None:
        self._probes.labels(category=str(category)).inc()

    def circuit_transition(self, target: str, state: str) -> None:
        self._circuits.labels(target=target, state=state).inc()

    def pool_state(self, domain: str, state: str, count: int) -> None:
        self._pool.labels(domain=domain, state=state).set(count)

    def tasks(self, domain: str, due: int, leased: int) -> None:
        self._tasks.labels(domain=domain, kind="due").set(due)
        self._tasks.labels(domain=domain, kind="leased").set(leased)

    def lease_reclaimed(self, domain: str, count: int) -> None:
        self._reclaimed.labels(domain=domain).inc(count)

    def api_request(self, route: str, method: str, status: str, duration: float) -> None:
        self._api.labels(route=route, method=method, status=status).observe(duration)

    def api_limit_rejected(self, scope: str) -> None:
        self._api_rejected.labels(scope=scope).inc()

    def redis_operation(self, operation: str, duration: float, *, error: bool) -> None:
        self._redis_duration.labels(operation=operation).observe(duration)
        if error:
            self._redis_errors.labels(operation=operation).inc()

    def heartbeat(self, role: str) -> None:
        self._heartbeat.labels(role=role).set_to_current_time()

    def peer_snapshot(self, domain: str, peer: str, snapshot: PeerMetricsSnapshot) -> None:
        self._peer_available.labels(domain=domain, peer=peer).set(int(snapshot.available))
        if not snapshot.available:
            for metric in (
                self._peer_valid,
                self._peer_failures,
                self._peer_heartbeat,
                self._peer_success,
                self._peer_evictions,
            ):
                metric.labels(domain=domain, peer=peer).set(float("nan"))
            return
        assert snapshot.valid_count is not None
        assert snapshot.consecutive_failures is not None
        assert snapshot.heartbeat_at is not None
        assert snapshot.last_success_at is not None
        assert snapshot.evictions_10m is not None
        self._peer_valid.labels(domain=domain, peer=peer).set(snapshot.valid_count)
        self._peer_failures.labels(domain=domain, peer=peer).set(snapshot.consecutive_failures)
        self._peer_heartbeat.labels(domain=domain, peer=peer).set(snapshot.heartbeat_at)
        self._peer_success.labels(domain=domain, peer=peer).set(snapshot.last_success_at)
        self._peer_evictions.labels(domain=domain, peer=peer).set(snapshot.evictions_10m)

    def proxy_selection(
        self,
        domain: str,
        *,
        index_members: int,
        candidates: int,
        requested: int,
        returned: int,
        skipped: Mapping[str, int],
        duration: float,
    ) -> None:
        outcome = "empty" if returned == 0 else "success" if returned >= requested else "partial"
        self._latency_index_members.labels(domain=domain).set(index_members)
        self._selectable_candidates.labels(domain=domain).set(candidates)
        self._selection_total.labels(domain=domain, outcome=outcome).inc()
        self._selection_returned.labels(domain=domain).observe(returned)
        for reason in ("score", "freshness", "successes", "inconsistent"):
            count = skipped.get(reason, 0)
            if count:
                self._selection_skipped.labels(domain=domain, reason=reason).inc(count)
        self._selection_duration.labels(domain=domain).observe(duration)


class NoopMetrics:
    def source_fetch(self, source: str, outcome: str) -> None:
        del source, outcome

    def source_parsed(self, source: str, count: int) -> None:
        del source, count

    def record_probe(self, category: str) -> None:
        del category

    def circuit_transition(self, target: str, state: str) -> None:
        del target, state

    def pool_state(self, domain: str, state: str, count: int) -> None:
        del domain, state, count

    def tasks(self, domain: str, due: int, leased: int) -> None:
        del domain, due, leased

    def lease_reclaimed(self, domain: str, count: int) -> None:
        del domain, count

    def api_request(self, route: str, method: str, status: str, duration: float) -> None:
        del route, method, status, duration

    def api_limit_rejected(self, scope: str) -> None:
        del scope

    def redis_operation(self, operation: str, duration: float, *, error: bool) -> None:
        del operation, duration, error

    def heartbeat(self, role: str) -> None:
        del role

    def peer_snapshot(self, domain: str, peer: str, snapshot: PeerMetricsSnapshot) -> None:
        del domain, peer, snapshot

    def proxy_selection(
        self,
        domain: str,
        *,
        index_members: int,
        candidates: int,
        requested: int,
        returned: int,
        skipped: Mapping[str, int],
        duration: float,
    ) -> None:
        del domain, index_members, candidates, requested, returned, skipped, duration


class OwnershipMetrics:
    """Low-cardinality ownership health, refreshed from Redis on scrape."""

    def __init__(self, *, registry: CollectorRegistry) -> None:
        self._heartbeat = Gauge(
            "ip_pool_reclaim_ownership_member_heartbeat_timestamp_seconds",
            "Last received ownership member heartbeat",
            ("member_id",),
            registry=registry,
        )
        self._success_backlog = Gauge(
            "ip_pool_reclaim_ownership_success_backlog",
            "Unpublished success events reported by member",
            ("member_id",),
            registry=registry,
        )
        self._pending = Gauge(
            "ip_pool_reclaim_ownership_pending_cases",
            "Pending ownership cases",
            registry=registry,
        )
        self._oldest = Gauge(
            "ip_pool_reclaim_ownership_oldest_pending_age_seconds",
            "Age of oldest pending ownership case",
            registry=registry,
        )
        self._check_errors = Gauge(
            "ip_pool_reclaim_ownership_check_errors",
            "Ownership member query errors",
            registry=registry,
        )
        self._conflicts = Gauge(
            "ip_pool_reclaim_ownership_revision_conflicts",
            "Ownership result and success conflicts",
            registry=registry,
        )

    def update(self, snapshot: Mapping[str, Any], member_ids: tuple[str, ...]) -> None:
        self._pending.set(float(snapshot["pending_count"]))
        self._oldest.set(float(snapshot["oldest_pending_age_seconds"]))
        self._check_errors.set(float(snapshot["check_errors"]))
        self._conflicts.set(float(snapshot["revision_conflicts"]))
        heartbeats = snapshot["heartbeats"]
        assert isinstance(heartbeats, dict)
        for member_id in member_ids:
            data = heartbeats.get(member_id) or {}
            self._heartbeat.labels(member_id=member_id).set(int(data.get("received_at", 0)) / 1000)
            self._success_backlog.labels(member_id=member_id).set(
                int(data.get("success_backlog", 0))
            )
