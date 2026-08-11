import pytest
from pydantic import TypeAdapter, ValidationError

from ip_proxy_pool.collector.models import (
    JsonSource,
    Pagination,
    RegexSource,
    SourceDefinition,
    XPathSource,
)
from ip_proxy_pool.collector.sources import (
    default_sources,
    sources_for_region,
    validate_sources,
)


def test_default_sources_have_unique_names_and_safe_limits() -> None:
    sources = default_sources()

    assert len(sources) == 7
    assert len({source.name for source in sources}) == len(sources)
    assert all(source.max_pages <= 20 for source in sources)
    assert all(source.urls for source in sources)
    assert all(str(url).startswith("https://") for source in sources for url in source.urls)


def test_foreign_region_excludes_other_regions() -> None:
    assert all(source.region in {"foreign", "all"} for source in sources_for_region("foreign"))


def test_default_catalog_has_no_domestic_source() -> None:
    assert sources_for_region("domestic") == []


def test_duplicate_source_names_are_rejected() -> None:
    source = default_sources()[0]

    with pytest.raises(ValueError, match="duplicate source name"):
        validate_sources([source, source])


def test_insecure_source_url_is_rejected() -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        RegexSource.model_validate(
            {
                "kind": "regex",
                "name": "unsafe",
                "region": "foreign",
                "urls": ["http://example.com/proxies"],
                "pattern": r"\d+:\d+",
            }
        )


def test_invalid_regex_is_rejected() -> None:
    with pytest.raises(ValidationError, match="regular expression"):
        RegexSource.model_validate(
            {
                "kind": "regex",
                "name": "broken",
                "region": "foreign",
                "urls": ["https://example.com/proxies"],
                "pattern": "[",
            }
        )


def test_pagination_template_requires_page_placeholder() -> None:
    with pytest.raises(ValidationError, match="page"):
        Pagination.model_validate(
            {
                "kind": "json_total",
                "url_template": "https://example.com/list?page=1",
            }
        )


def test_parser_models_require_their_fields() -> None:
    adapter = TypeAdapter(SourceDefinition)
    common = {
        "name": "missing",
        "region": "foreign",
        "urls": ["https://example.com/list"],
    }

    for kind in ("json", "xpath"):
        with pytest.raises(ValidationError):
            adapter.validate_python({**common, "kind": kind})


def test_source_union_selects_exact_parser_model() -> None:
    by_name = {source.name: source for source in default_sources()}

    assert isinstance(by_name["geonode"], JsonSource)
    assert isinstance(by_name["thespeedx"], RegexSource)
    assert isinstance(by_name["freeproxy-world"], XPathSource)
