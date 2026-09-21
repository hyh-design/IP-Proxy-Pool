from collections.abc import Iterable
from typing import Literal

from ip_proxy_pool.collector.models import (
    JsonSource,
    Pagination,
    RegexSource,
    SourceDefinition,
    XPathSource,
)

_ENDPOINT_PATTERN = r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}(?!\d)"


def default_sources() -> list[SourceDefinition]:
    sources: list[SourceDefinition] = [
        JsonSource.model_validate(
            {
                "name": "geonode",
                "region": "foreign",
                "urls": [
                    "https://proxylist.geonode.com/api/proxy-list?limit=500&page=1&sort_by=lastChecked&sort_type=desc&protocols=http%2Chttps"
                ],
                "max_pages": 20,
                "pagination": Pagination.model_validate(
                    {
                        "kind": "json_total",
                        "url_template": "https://proxylist.geonode.com/api/proxy-list?limit=500&page={page}&sort_by=lastChecked&sort_type=desc&protocols=http%2Chttps",
                        "total_field": "total",
                        "limit_field": "limit",
                    }
                ),
                "list_path": "data",
                "ip_field": "ip",
                "port_field": "port",
            }
        ),
        RegexSource.model_validate(
            {
                "name": "thespeedx",
                "region": "foreign",
                "urls": ["https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt"],
                "pattern": _ENDPOINT_PATTERN,
            }
        ),
        RegexSource.model_validate(
            {
                "name": "monosans",
                "region": "foreign",
                "urls": [
                    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt"
                ],
                "pattern": _ENDPOINT_PATTERN,
            }
        ),
        RegexSource.model_validate(
            {
                "name": "zaeem20",
                "region": "foreign",
                "urls": [
                    "https://raw.githubusercontent.com/Zaeem20/FREE_PROXIES_LIST/master/http.txt"
                ],
                "pattern": _ENDPOINT_PATTERN,
            }
        ),
        XPathSource.model_validate(
            {
                "name": "66daili",
                "region": "foreign",
                "urls": ["https://www.66daili.com/?protocol=HTTPS&page=1&size=60"],
                "max_pages": 1,
                "row_xpath": (
                    ".//div[contains(concat(' ', normalize-space(@class), ' '), "
                    "' table-sj ')]/ul[li[4][normalize-space()='高匿' or "
                    "normalize-space()='普匿' or normalize-space()='未知'] and "
                    "li[5][normalize-space()='HTTPS']]"
                ),
                "ip_xpath": "./li[1]",
                "port_xpath": "./li[2]",
            }
        ),
        XPathSource.model_validate(
            {
                "name": "freeproxy-world",
                "region": "foreign",
                "urls": [
                    "https://www.freeproxy.world/?type=http&anonymity=&country=&speed=&port=&page=1"
                ],
                "max_pages": 20,
                "pagination": Pagination.model_validate(
                    {
                        "kind": "html_max_page",
                        "url_template": "https://www.freeproxy.world/?type=http&anonymity=&country=&speed=&port=&page={page}",
                        "page_xpath": "//*[contains(@class, 'pagination')]//a/text()",
                    }
                ),
                "row_xpath": ".//table//tr[td]",
                "ip_xpath": "./td[1]",
                "port_xpath": "./td[2]",
            }
        ),
    ]
    return validate_sources(sources)


def validate_sources(sources: Iterable[SourceDefinition]) -> list[SourceDefinition]:
    result = list(sources)
    names = [source.name for source in result]
    if len(names) != len(set(names)):
        raise ValueError("duplicate source name")
    return result


def sources_for_region(
    region: Literal["domestic", "foreign", "all"] | str,
) -> list[SourceDefinition]:
    if region not in {"domestic", "foreign", "all"}:
        raise ValueError("region must be domestic, foreign, or all")
    if region == "all":
        return default_sources()
    return [source for source in default_sources() if source.region in {region, "all"}]
