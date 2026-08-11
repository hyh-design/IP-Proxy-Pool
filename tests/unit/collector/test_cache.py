from datetime import UTC, datetime

import fakeredis.aioredis

from ip_proxy_pool.collector.cache import SourceCache, source_cache_key
from ip_proxy_pool.collector.downloader import DownloadResult


def test_cache_digest_does_not_expose_query_secrets() -> None:
    key = source_cache_key("ippool:test", "https://x.example/list?token=secret")

    assert "secret" not in key
    assert key.startswith("ippool:test:source-cache:page:")


async def test_page_round_trip_sets_ttl() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = SourceCache(client, prefix="ippool:test")
    url = "https://source.example/list"
    result = DownloadResult(
        payload=b"payload",
        content_type="text/plain",
        fetched_at=datetime(2026, 8, 10, tzinfo=UTC),
    )
    try:
        await cache.set_page(url, result, ttl=60)

        restored = await cache.get_page(url)
        ttl = await client.ttl(source_cache_key("ippool:test", url))

        assert restored == result
        assert 0 < ttl <= 60
    finally:
        await client.aclose()


async def test_corrupted_cache_is_deleted_and_treated_as_miss() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = SourceCache(client, prefix="ippool:test")
    url = "https://source.example/list"
    key = source_cache_key("ippool:test", url)
    try:
        await client.set(key, "not-json")

        assert await cache.get_page(url) is None
        assert await client.exists(key) == 0
    finally:
        await client.aclose()


async def test_page_and_pagination_metadata_are_independent() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    cache = SourceCache(client, prefix="ippool:test")
    url = "https://source.example/list?page=1"
    result = DownloadResult(
        payload=b"first",
        content_type="application/json",
        fetched_at=datetime(2026, 8, 10, tzinfo=UTC),
    )
    pages = [url, "https://source.example/list?page=2"]
    try:
        await cache.set_page(url, result, ttl=60)
        await cache.set_pagination("source-a", pages, ttl=30)

        assert await cache.get_page(url) == result
        assert await cache.get_pagination("source-a") == pages
    finally:
        await client.aclose()
