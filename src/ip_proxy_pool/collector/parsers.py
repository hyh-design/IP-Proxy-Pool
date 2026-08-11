import json
import re
from collections.abc import Iterable
from typing import Any, cast

from lxml import etree, html

from ip_proxy_pool.collector.models import (
    JsonSource,
    RegexSource,
    SourceDefinition,
    XPathSource,
)
from ip_proxy_pool.models import ProxyEndpoint


def _endpoint_text(host: object, port: object) -> str:
    raw_host = str(host).strip()
    raw_port = str(port).strip()
    return f"[{raw_host}]:{raw_port}" if ":" in raw_host else f"{raw_host}:{raw_port}"


def _valid_unique(candidates: Iterable[str]) -> list[ProxyEndpoint]:
    endpoints: list[ProxyEndpoint] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            endpoint = ProxyEndpoint.parse(candidate)
        except (TypeError, ValueError):
            continue
        if endpoint.canonical in seen:
            continue
        seen.add(endpoint.canonical)
        endpoints.append(endpoint)
    return endpoints


def _json_candidates(payload: bytes, source: JsonSource) -> Iterable[str]:
    try:
        value: Any = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return []
    for component in source.list_path.split("."):
        if not isinstance(value, dict) or component not in value:
            return []
        value = value[component]
    if not isinstance(value, list):
        return []

    candidates: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if source.ip_field not in item or source.port_field not in item:
            continue
        candidates.append(_endpoint_text(item[source.ip_field], item[source.port_field]))
    return candidates


def _node_text(node: etree._Element, expression: str) -> str | None:
    values = cast(list[Any], node.xpath(expression))
    if not values:
        return None
    first = values[0]
    if isinstance(first, etree._Element):
        parts = (
            part.decode("utf-8", errors="replace") if isinstance(part, bytes) else part
            for part in first.itertext()
        )
        return "".join(parts).strip()
    return str(first).strip()


def _xpath_candidates(payload: bytes, source: XPathSource) -> Iterable[str]:
    if not payload:
        return []
    try:
        root = html.fromstring(payload)
        rows = cast(list[Any], root.xpath(source.row_xpath))
    except (etree.ParserError, etree.XMLSyntaxError, TypeError, ValueError):
        return []

    candidates: list[str] = []
    for row in rows:
        if not isinstance(row, etree._Element):
            continue
        host = _node_text(row, source.ip_xpath)
        port = _node_text(row, source.port_xpath)
        if host and port:
            candidates.append(_endpoint_text(host, port))
    return candidates


def parse_source(
    payload: bytes,
    source: SourceDefinition,
) -> list[ProxyEndpoint]:
    """Parse a remote payload while discarding malformed endpoint candidates."""
    if isinstance(source, JsonSource):
        candidates = _json_candidates(payload, source)
    elif isinstance(source, RegexSource):
        candidates = re.findall(source.pattern, payload.decode("utf-8", errors="replace"))
    elif isinstance(source, XPathSource):
        candidates = _xpath_candidates(payload, source)
    else:
        return []
    return _valid_unique(candidates)
