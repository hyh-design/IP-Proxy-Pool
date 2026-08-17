from ip_proxy_pool.benchmark import summarize


def test_benchmark_summary_reports_hand_checked_percentiles_and_failures() -> None:
    summary = summarize(
        durations_ms=[1.0, 2.0, 3.0, 4.0, 100.0],
        returned_counts=[20, 20, 0, 12, 20],
    )

    assert summary == {
        "iterations": 5,
        "returned_min": 0,
        "empty_count": 1,
        "p50_ms": 3.0,
        "p95_ms": 100.0,
    }
