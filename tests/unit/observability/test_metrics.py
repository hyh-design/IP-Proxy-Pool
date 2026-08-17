import pytest
from prometheus_client import CollectorRegistry

from ip_proxy_pool.observability.metrics import (
    PrometheusMetrics,
    validate_metric_labels,
)


@pytest.mark.parametrize(
    "label",
    ["endpoint", "url", "query", "error_message", "api_key", "token"],
)
def test_metrics_reject_high_cardinality_or_secret_labels(label: str) -> None:
    with pytest.raises(ValueError, match="forbidden label"):
        validate_metric_labels({label: "value"})


def test_representative_metrics_increment_without_endpoint_labels() -> None:
    registry = CollectorRegistry()
    metrics = PrometheusMetrics(registry=registry)

    metrics.source_fetch("geonode", "success")
    metrics.record_probe("proxy_error")
    metrics.circuit_transition("example", "open")
    metrics.api_limit_rejected("query")
    metrics.redis_operation("claim", 0.01, error=True)
    metrics.lease_reclaimed("example.com", 2)
    metrics.proxy_selection(
        "example.com",
        index_members=38,
        candidates=30,
        requested=20,
        returned=20,
        skipped={"score": 2, "freshness": 3, "successes": 4, "inconsistent": 1},
        duration=0.01,
    )

    names = {sample.name for metric in registry.collect() for sample in metric.samples}
    assert "ip_pool_source_fetch_total" in names
    assert "ip_pool_probe_total" in names
    assert "ip_pool_api_limit_rejected_total" in names
    assert "ip_pool_redis_error_total" in names
    assert "ip_pool_lease_reclaimed_total" in names
    assert "ip_pool_latency_index_members" in names
    assert "ip_pool_selectable_candidates" in names
    assert "ip_pool_proxy_selection_total" in names
    assert "ip_pool_proxy_selection_returned_count" in names
    assert "ip_pool_proxy_selection_skipped_total" in names
    assert "ip_pool_proxy_selection_duration_seconds_count" in names
