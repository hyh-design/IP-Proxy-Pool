import os
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    client = Redis.from_url(
        os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15"),
        decode_responses=True,
    )
    try:
        await client.ping()
    except RedisError as error:
        await client.aclose()
        pytest.fail(f"real Redis required for docker-marked test: {error}")

    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()
