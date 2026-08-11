import asyncio

import httpx
import pytest

from ip_proxy_pool.collector.downloader import (
    ResponseTooLarge,
    SourceDownloader,
)


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for chunk in self.chunks:
            yield chunk


class SequenceTransport(httpx.AsyncBaseTransport):
    def __init__(self, outcomes: list[httpx.Response | Exception]) -> None:
        self.outcomes = outcomes
        self.requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        outcome.request = request
        return outcome


async def no_sleep(delay: float) -> None:
    del delay
    await asyncio.sleep(0)


async def test_downloader_aborts_when_stream_exceeds_limit() -> None:
    response = httpx.Response(
        200,
        stream=ChunkStream([b"a" * 700, b"b" * 700]),
    )
    downloader = SourceDownloader(
        transport=SequenceTransport([response]),
        max_bytes=1024,
        timeout=2.0,
        sleep=no_sleep,
    )
    try:
        with pytest.raises(ResponseTooLarge):
            await downloader.fetch("https://source.example/list")
    finally:
        await downloader.close()


async def test_content_length_is_rejected_before_body_read() -> None:
    response = httpx.Response(
        200,
        headers={"content-length": "2048"},
        stream=ChunkStream([b"not-read"]),
    )
    downloader = SourceDownloader(
        transport=SequenceTransport([response]), max_bytes=1024, timeout=2.0
    )
    try:
        with pytest.raises(ResponseTooLarge):
            await downloader.fetch("https://source.example/list")
    finally:
        await downloader.close()


async def test_connect_timeout_and_retryable_status_are_retried() -> None:
    transport = SequenceTransport(
        [
            httpx.ConnectTimeout("timeout"),
            httpx.Response(503),
            httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"}),
        ]
    )
    downloader = SourceDownloader(
        transport=transport,
        max_bytes=1024,
        timeout=2.0,
        sleep=no_sleep,
    )
    try:
        result = await downloader.fetch("https://source.example/list")
    finally:
        await downloader.close()

    assert result.payload == b"ok"
    assert result.content_type == "text/plain"
    assert transport.requests == 3


async def test_non_retryable_4xx_fails_once() -> None:
    transport = SequenceTransport([httpx.Response(404)])
    downloader = SourceDownloader(
        transport=transport,
        max_bytes=1024,
        timeout=2.0,
        sleep=no_sleep,
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await downloader.fetch("https://source.example/list")
    finally:
        await downloader.close()

    assert transport.requests == 1
