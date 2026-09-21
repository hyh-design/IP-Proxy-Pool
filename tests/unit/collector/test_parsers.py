from pathlib import Path

import pytest

from ip_proxy_pool.collector.models import SourceDefinition
from ip_proxy_pool.collector.parsers import parse_source
from ip_proxy_pool.collector.sources import default_sources

FIXTURE_DIRECTORY = Path(__file__).parents[2] / "fixtures" / "sources"


@pytest.fixture
def source_map() -> dict[str, SourceDefinition]:
    return {source.name: source for source in default_sources()}


@pytest.mark.parametrize(
    ("fixture_name", "source_name", "expected"),
    [
        ("geonode.json", "geonode", ["1.1.1.1:80"]),
        ("proxy-list.txt", "thespeedx", ["8.8.8.8:8080"]),
        ("freeproxy-world.html", "freeproxy-world", ["9.9.9.9:3128"]),
        (
            "66daili.html",
            "66daili",
            ["47.95.206.224:45002", "3.3.3.3:8082", "4.4.4.4:8083"],
        ),
    ],
)
def test_parsers_return_valid_unique_endpoints(
    fixture_name: str,
    source_name: str,
    expected: list[str],
    source_map: dict[str, SourceDefinition],
) -> None:
    payload = (FIXTURE_DIRECTORY / fixture_name).read_bytes()

    endpoints = parse_source(payload, source_map[source_name])

    assert [item.canonical for item in endpoints] == expected


@pytest.mark.parametrize(
    ("source_name", "payload"),
    [
        ("geonode", b"not-json"),
        ("geonode", b'{"data": {"not": "a-list"}}'),
        ("freeproxy-world", b"<not-closed"),
        ("thespeedx", b""),
    ],
)
def test_malformed_or_empty_payload_is_safe(
    source_name: str,
    payload: bytes,
    source_map: dict[str, SourceDefinition],
) -> None:
    assert parse_source(payload, source_map[source_name]) == []


def test_regex_parser_retains_valid_subset_with_malformed_utf8(
    source_map: dict[str, SourceDefinition],
) -> None:
    payload = b"\xff1.1.1.1:80\n999.1.1.1:80\n1.1.1.1:70000"

    endpoints = parse_source(payload, source_map["thespeedx"])

    assert [item.canonical for item in endpoints] == ["1.1.1.1:80"]
