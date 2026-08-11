import json
import math
from typing import Any, cast

from lxml import etree, html

from ip_proxy_pool.collector.models import SourceDefinition


def _positive_integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(cast(Any, value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def discover_pages(
    source: SourceDefinition,
    first_payload: bytes,
    global_max_pages: int,
) -> list[str]:
    """Discover pagination without trusting remote page-count metadata."""
    if global_max_pages < 1:
        raise ValueError("global_max_pages must be positive")
    configured_urls = [str(url) for url in source.urls]
    cap = min(source.max_pages, global_max_pages)
    pagination = source.pagination
    if pagination is None:
        return configured_urls[:cap]

    first_url = configured_urls[0]
    page_count: int | None = None
    if pagination.kind == "json_total":
        try:
            metadata = json.loads(first_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return [first_url]
        if not isinstance(metadata, dict):
            return [first_url]
        total = _positive_integer(metadata.get(pagination.total_field))
        limit = _positive_integer(metadata.get(pagination.limit_field))
        if total is None or limit is None:
            return [first_url]
        page_count = max(1, math.ceil(total / limit))
    else:
        if not pagination.page_xpath or not first_payload:
            return [first_url]
        try:
            root = html.fromstring(first_payload)
            labels = cast(list[Any], root.xpath(pagination.page_xpath))
        except (etree.ParserError, etree.XMLSyntaxError, TypeError, ValueError):
            return [first_url]
        page_numbers = [
            number
            for label in labels
            if (number := _positive_integer(str(label).strip())) is not None
        ]
        if not page_numbers:
            return [first_url]
        page_count = max(page_numbers)

    bounded_count = min(page_count, cap)
    pages = [
        pagination.url_template.replace("{page}", str(page)) for page in range(1, bounded_count + 1)
    ]
    pages[0] = first_url
    return pages
