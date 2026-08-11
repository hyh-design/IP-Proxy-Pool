import pytest

from ip_proxy_pool.api.cursors import CursorCodec, InvalidCursor


def test_cursor_round_trip() -> None:
    codec = CursorCodec(secret=b"cursor-secret")

    cursor = codec.encode(domain="example.com", offset=50, filters_hash="abc")
    decoded = codec.decode(cursor, expected_domain="example.com", expected_filters_hash="abc")

    assert decoded.offset == 50


def test_cursor_rejects_tampering() -> None:
    codec = CursorCodec(secret=b"cursor-secret")
    cursor = codec.encode(domain="example.com", offset=50, filters_hash="abc")
    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")

    with pytest.raises(InvalidCursor):
        codec.decode(tampered)


def test_cursor_rejects_domain_and_filter_mismatch() -> None:
    codec = CursorCodec(secret=b"cursor-secret")
    cursor = codec.encode(domain="example.com", offset=50, filters_hash="abc")

    with pytest.raises(InvalidCursor):
        codec.decode(cursor, expected_domain="other.example", expected_filters_hash="abc")
    with pytest.raises(InvalidCursor):
        codec.decode(cursor, expected_domain="example.com", expected_filters_hash="xyz")


@pytest.mark.parametrize("offset", [-1, 10_001])
def test_cursor_offset_is_bounded(offset: int) -> None:
    codec = CursorCodec(secret=b"cursor-secret")

    with pytest.raises(ValueError):
        codec.encode(domain="example.com", offset=offset, filters_hash="abc")


def test_invalid_payload_is_rejected() -> None:
    codec = CursorCodec(secret=b"cursor-secret")

    with pytest.raises(InvalidCursor):
        codec.decode("not-a-cursor")
