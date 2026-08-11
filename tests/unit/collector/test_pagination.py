from ip_proxy_pool.collector.models import JsonSource, RegexSource, XPathSource
from ip_proxy_pool.collector.pagination import discover_pages
from ip_proxy_pool.collector.sources import default_sources


def source(name: str) -> JsonSource | RegexSource | XPathSource:
    return next(item for item in default_sources() if item.name == name)


def test_json_total_is_capped_by_source_and_global_limits() -> None:
    geonode = source("geonode")
    payload = b'{"total": 999999999, "limit": 1, "data": []}'

    pages = discover_pages(geonode, payload, global_max_pages=20)

    assert len(pages) == 20
    assert pages[0] == str(geonode.urls[0])
    assert "page=20" in pages[-1]


def test_smaller_source_limit_wins() -> None:
    geonode = source("geonode").model_copy(update={"max_pages": 3})

    pages = discover_pages(
        geonode,
        b'{"total": 100, "limit": 1, "data": []}',
        global_max_pages=20,
    )

    assert len(pages) == 3


def test_invalid_json_or_zero_limit_returns_first_url_only() -> None:
    geonode = source("geonode")

    assert discover_pages(geonode, b"bad", global_max_pages=20) == [str(geonode.urls[0])]
    assert discover_pages(
        geonode,
        b'{"total": 100, "limit": 0}',
        global_max_pages=20,
    ) == [str(geonode.urls[0])]


def test_giant_html_page_number_is_capped() -> None:
    html_source = source("freeproxy-world")
    payload = b'<nav class="pagination"><a>1</a><a>999999999</a></nav>'

    pages = discover_pages(html_source, payload, global_max_pages=7)

    assert len(pages) == 7
    assert pages[0] == str(html_source.urls[0])
    assert "page=7" in pages[-1]


def test_invalid_html_metadata_returns_first_url_only() -> None:
    html_source = source("freeproxy-world")

    assert discover_pages(html_source, b"<html><p>none</p></html>", global_max_pages=20) == [
        str(html_source.urls[0])
    ]


def test_non_paginated_source_returns_configured_urls_with_cap() -> None:
    regex_source = source("thespeedx").model_copy(
        update={
            "urls": (
                "https://example.com/1",
                "https://example.com/2",
                "https://example.com/3",
            )
        }
    )

    assert discover_pages(regex_source, b"", global_max_pages=2) == [
        "https://example.com/1",
        "https://example.com/2",
    ]
