from datetime import UTC, datetime

import pytest

from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord
from ip_proxy_pool.storage.codec import decode_record, encode_record


def test_record_json_round_trip() -> None:
    now = datetime.now(UTC)
    record = ProxyRecord(
        endpoint=ProxyEndpoint.parse("1.1.1.1:80"),
        domain="example.com",
        source_names={"source-b", "source-a"},
        first_seen_at=now,
        last_seen_at=now,
        next_check_at=now,
    )

    assert decode_record(encode_record(record)) == record


def test_invalid_record_json_is_rejected() -> None:
    with pytest.raises(ValueError):
        decode_record('{"endpoint":"not-an-endpoint"}')
