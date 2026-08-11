from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, Route, expect

pytestmark = pytest.mark.docker

SUMMARY_RESPONSE = {
    "domain": "example.com",
    "observed_at": "2026-08-11T08:00:00Z",
    "total": 2,
    "candidate": 0,
    "available": 1,
    "degraded": 1,
    "quarantined": 0,
    "due": 2,
    "leased": 0,
    "availability_rate": 0.5,
    "available_pool_share": 0.5,
    "high_quality": 1,
    "latest_snapshot_at": "2026-08-11T08:00:00Z",
    "freshness_seconds": 0,
    "api_status": "healthy",
    "redis_status": "healthy",
    "collector": {"role": "collector", "status": "healthy", "active_instances": 1},
    "checker": {"role": "checker", "status": "healthy", "active_instances": 1},
    "scanned": 2,
    "partial": False,
}

QUALITY_PARTIAL = {
    "domain": "example.com",
    "observed_at": "2026-08-11T08:00:00Z",
    "candidate": 0,
    "available": 1,
    "degraded": 1,
    "quarantined": 0,
    "score_buckets": {"low": 0, "watch": 1, "usable": 0, "high": 1},
    "latency": {"samples": 2, "average_ms": 165, "p50_ms": 90, "p95_ms": 240},
    "scanned": 20_000,
    "partial": True,
}


def test_key_login_loads_default_24h_dashboard(page: Page, dashboard_url: str) -> None:
    page.goto(dashboard_url)
    page.get_by_label("API Key").fill("read-key")
    page.get_by_role("button", name="进入仪表盘").click()

    expect(page.get_by_test_id("range-24h")).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_test_id("availability-rate")).to_have_text("50.0%")
    assert page.evaluate("sessionStorage.getItem('ipPoolDashboardKey')") == "read-key"
    expect(page.locator("#proxy-details")).not_to_have_attribute("open", "")


def test_range_switch_requests_12h_and_draws_gap(authenticated_page: Page) -> None:
    authenticated_page.get_by_test_id("range-12h").click()

    expect(authenticated_page.locator("#trend-chart")).to_have_attribute("data-resolution", "5m")
    expect(authenticated_page.locator("#trend-chart [data-gap='true']")).to_have_count(3)


def test_all_supported_ranges_are_selectable(authenticated_page: Page) -> None:
    for range_name, resolution in (
        ("1h", "5m"),
        ("6h", "5m"),
        ("7d", "1h"),
        ("30d", "1h"),
    ):
        button = authenticated_page.get_by_test_id(f"range-{range_name}")
        button.click()
        expect(button).to_have_attribute("aria-pressed", "true")
        expect(authenticated_page.locator("#trend-chart")).to_have_attribute(
            "data-resolution", resolution
        )


def test_invalid_key_clears_previous_data(page: Page, dashboard_url: str) -> None:
    calls = 0

    def handle_summary(route: Route) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            route.fulfill(status=200, json=SUMMARY_RESPONSE)
        else:
            route.fulfill(status=401, json={"detail": "invalid API key"})

    page.route("**/v1/dashboard/summary*", handle_summary)
    page.goto(dashboard_url)
    page.get_by_label("API Key").fill("read-key")
    page.get_by_role("button", name="进入仪表盘").click()
    expect(page.get_by_test_id("availability-rate")).to_be_visible()

    page.get_by_role("button", name="立即刷新").click()

    expect(page.locator("#auth-panel")).to_be_visible()
    expect(page.locator("#kpi-grid")).to_be_empty()
    assert page.evaluate("sessionStorage.getItem('ipPoolDashboardKey')") is None


def test_proxy_filters_and_cursor_pagination(
    authenticated_page: Page,
) -> None:
    requests: list[dict[str, list[str]]] = []

    def handle_proxies(route: Route) -> None:
        query = parse_qs(urlparse(route.request.url).query)
        requests.append(query)
        cursor = query.get("cursor", [None])[0]
        endpoint = "2.2.2.2:8080" if cursor else "1.1.1.1:80"
        route.fulfill(
            status=200,
            json={
                "items": [
                    {
                        "endpoint": endpoint,
                        "domain": "example.com",
                        "score": 92,
                        "state": "available",
                        "source_names": ["alpha"],
                        "latency_ewma_ms": 90,
                        "last_checked_at": "2026-08-11T08:00:00Z",
                        "next_check_at": "2026-08-11T08:05:00Z",
                    }
                ],
                "next_cursor": None if cursor else "signed-cursor",
            },
        )

    authenticated_page.route("**/v1/proxies?*", handle_proxies)
    authenticated_page.locator("#proxy-details").get_by_text("代理明细").click()
    authenticated_page.locator("#proxy-state").select_option("available")
    authenticated_page.locator("#proxy-min-score").fill("80")
    authenticated_page.locator("#proxy-max-score").fill("95")
    authenticated_page.locator("#proxy-source").fill("alpha")
    authenticated_page.get_by_role("button", name="应用筛选").click()

    expect(authenticated_page.locator("#proxy-table-region")).to_contain_text("1.1.1.1:80")
    authenticated_page.get_by_role("button", name="下一页").click()
    expect(authenticated_page.locator("#proxy-table-region")).to_contain_text("2.2.2.2:8080")
    assert requests[-1]["cursor"] == ["signed-cursor"]
    filtered_request = requests[-2]
    assert filtered_request["state"] == ["available"]
    assert filtered_request["min_score"] == ["80"]
    assert filtered_request["max_score"] == ["95"]
    assert filtered_request["source"] == ["alpha"]


def test_reduced_motion_and_keyboard_focus(page: Page, dashboard_url: str) -> None:
    page.emulate_media(reduced_motion="reduce")
    page.goto(dashboard_url)
    page.get_by_label("API Key").fill("read-key")
    page.get_by_role("button", name="进入仪表盘").click()

    expect(page.locator("#trend-chart")).to_have_attribute("data-reduced-motion", "true")
    page.get_by_test_id("range-24h").focus()
    assert page.get_by_test_id("range-24h").evaluate(
        "element => element === document.activeElement"
    )


def test_hidden_page_pauses_polling(authenticated_page: Page) -> None:
    expect(authenticated_page.locator("#dashboard")).to_have_attribute("data-polling", "active")

    authenticated_page.evaluate(
        "Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});"
        "document.dispatchEvent(new Event('visibilitychange'));"
    )

    expect(authenticated_page.locator("#dashboard")).to_have_attribute("data-polling", "paused")


def test_refresh_failures_back_off_30_60_120_seconds(page: Page, dashboard_url: str) -> None:
    calls = 0

    def handle_summary(route: Route) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            route.fulfill(status=200, json=SUMMARY_RESPONSE)
        else:
            route.fulfill(status=503, json={"detail": "service unavailable"})

    page.route("**/v1/dashboard/summary*", handle_summary)
    page.goto(dashboard_url)
    page.get_by_label("API Key").fill("read-key")
    page.get_by_role("button", name="进入仪表盘").click()
    expect(page.get_by_test_id("availability-rate")).to_be_visible()

    page.get_by_role("button", name="立即刷新").click()
    expect(page.locator("#dashboard-content")).to_be_hidden()
    expect(page.locator("#dashboard")).to_have_attribute("data-refresh-delay", "30")
    for expected_delay in ("60", "120"):
        page.get_by_role("button", name="立即刷新").click()
        expect(page.locator("#dashboard")).to_have_attribute("data-refresh-delay", expected_delay)


def test_partial_stale_and_down_states_are_explicit(page: Page, dashboard_url: str) -> None:
    stale_summary = {
        **SUMMARY_RESPONSE,
        "freshness_seconds": 2000,
        "collector": {
            "role": "collector",
            "status": "stale",
            "active_instances": 1,
        },
        "checker": {"role": "checker", "status": "down", "active_instances": 0},
    }
    page.route(
        "**/v1/dashboard/summary*",
        lambda route: route.fulfill(status=200, json=stale_summary),
    )
    page.route(
        "**/v1/dashboard/quality*",
        lambda route: route.fulfill(status=200, json=QUALITY_PARTIAL),
    )
    page.goto(dashboard_url)
    page.get_by_label("API Key").fill("read-key")
    page.get_by_role("button", name="进入仪表盘").click()

    expect(page.locator("#freshness-badge")).to_have_text("监测中断")
    expect(page.locator("#health-strip")).to_contain_text("采集 延迟")
    expect(page.locator("#health-strip")).to_contain_text("检测 离线")
    expect(page.locator("#quality-sample")).to_contain_text("基于部分样本")


def test_quality_failure_does_not_hide_successful_summary(page: Page, dashboard_url: str) -> None:
    page.route(
        "**/v1/dashboard/summary*",
        lambda route: route.fulfill(status=200, json=SUMMARY_RESPONSE),
    )
    page.route(
        "**/v1/dashboard/quality*",
        lambda route: route.fulfill(status=503, json={"detail": "service unavailable"}),
    )
    page.goto(dashboard_url)
    page.get_by_label("API Key").fill("read-key")
    page.get_by_role("button", name="进入仪表盘").click()

    expect(page.get_by_test_id("availability-rate")).to_have_text("50.0%")
    expect(page.locator("#score-distribution")).to_contain_text("质量数据暂不可用")
