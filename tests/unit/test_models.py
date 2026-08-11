from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("1.2.3.4:8080", "1.2.3.4:8080"),
        ("[2001:4860:4860::8888]:443", "[2001:4860:4860::8888]:443"),
    ],
)
def test_proxy_endpoint_canonical_form(raw: str, canonical: str) -> None:
    assert ProxyEndpoint.parse(raw).canonical == canonical


@pytest.mark.parametrize(
    "raw",
    [
        "2001:4860:4860::8888:443",
        "1.2.3.4:0",
        "1.2.3.4:65536",
        "hostname.example:80",
    ],
)
def test_proxy_endpoint_rejects_ambiguous_or_invalid_values(raw: str) -> None:
    with pytest.raises((ValueError, ValidationError)):
        ProxyEndpoint.parse(raw)


def test_proxy_record_starts_as_candidate() -> None:
    now = datetime.now(UTC)

    record = ProxyRecord(
        endpoint=ProxyEndpoint.parse("1.1.1.1:80"),
        domain="example.com",
        first_seen_at=now,
        last_seen_at=now,
        next_check_at=now,
    )

    assert record.state is ProxyState.CANDIDATE
    assert record.score == 50
