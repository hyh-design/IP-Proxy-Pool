from collections.abc import Mapping
from typing import Protocol

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram

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
