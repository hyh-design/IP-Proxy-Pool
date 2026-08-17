import argparse
import asyncio
import json
import math
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.asyncio import Redis

from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState
from ip_proxy_pool.storage.codec import encode_record
from ip_proxy_pool.storage.keys import keys_for
from ip_proxy_pool.storage.repository import LATENCY_INDEX_SCHEMA_VERSION, RedisRepository


def summarize(durations_ms: list[float], returned_counts: list[int]) -> dict[str, Any]:
    if not durations_ms or len(durations_ms) != len(returned_counts):
        raise ValueError("benchmark samples must be non-empty and aligned")
    ordered = sorted(durations_ms)

    def percentile(fraction: float) -> float:
        index = max(0, math.ceil(len(ordered) * fraction) - 1)
        return round(ordered[index], 3)

    return {
        "iterations": len(ordered),
        "returned_min": min(returned_counts),
        "empty_count": sum(count == 0 for count in returned_counts),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark latency-index proxy selection")
    parser.add_argument("--redis-url", default="redis://127.0.0.1:6379/15")
    parser.add_argument("--key-prefix", default="ippool:benchmark")
    parser.add_argument("--domain", default="portal.daqihui.com")
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--max-latency-ms", type=float, default=1000.0)
    parser.add_argument("--min-score", type=int, default=80)
    return parser


async def _delete_prefix(redis: Redis, prefix: str) -> None:
    batch: list[str] = []
    async for key in redis.scan_iter(match=f"{prefix}:*", count=500):
        batch.append(str(key))
        if len(batch) == 500:
            await redis.delete(*batch)
            batch.clear()
    if batch:
        await redis.delete(*batch)


async def run_benchmark(arguments: argparse.Namespace) -> dict[str, Any]:
    if "benchmark" not in arguments.key_prefix.lower():
        raise ValueError("benchmark key prefix must contain 'benchmark'")
    if arguments.iterations < 1:
        raise ValueError("iterations must be positive")
    redis = Redis.from_url(arguments.redis_url, decode_responses=True)
    repository = RedisRepository(redis, prefix=arguments.key_prefix)
    keys = keys_for(arguments.key_prefix, arguments.domain)
    now = datetime.now(UTC)
    base = ProxyRecord(
        endpoint=ProxyEndpoint.parse("10.0.0.1:80"),
        domain=arguments.domain,
        score=90,
        state=ProxyState.AVAILABLE,
        source_names={"selection-benchmark"},
        first_seen_at=now,
        last_seen_at=now,
        last_checked_at=now,
        next_check_at=now + timedelta(minutes=5),
        consecutive_successes=2,
        latency_ewma_ms=500.0,
    )
    try:
        await _delete_prefix(redis, arguments.key_prefix)
        pipeline = redis.pipeline(transaction=False)
        for number in range(10_000):
            endpoint = ProxyEndpoint.parse(
                f"10.{number // 65_536}.{(number // 256) % 256}.{number % 256}:80"
            )
            latency = 500.0 if number >= 9962 else 1500.0
            record = base.model_copy(update={"endpoint": endpoint, "latency_ewma_ms": latency})
            canonical = endpoint.canonical
            pipeline.hset(keys.records, canonical, encode_record(record))
            pipeline.zadd(keys.quality, {canonical: record.score})
            pipeline.zadd(keys.available_latency, {canonical: latency})
            if (number + 1) % 500 == 0:
                await pipeline.execute()
                pipeline = redis.pipeline(transaction=False)
        pipeline.sadd(f"{arguments.key_prefix}:domains", arguments.domain)
        pipeline.set(keys.available_latency_ready, LATENCY_INDEX_SCHEMA_VERSION)
        await pipeline.execute()

        durations_ms: list[float] = []
        returned_counts: list[int] = []
        for _ in range(arguments.iterations):
            started = time.perf_counter()
            selected = await repository.random_proxies(
                arguments.domain,
                min_score=arguments.min_score,
                count=20,
                max_latency_ms=arguments.max_latency_ms,
                max_checked_age_seconds=600,
                min_consecutive_successes=2,
                now=now,
            )
            durations_ms.append((time.perf_counter() - started) * 1000)
            returned_counts.append(len(selected))
        return summarize(durations_ms, returned_counts)
    finally:
        await _delete_prefix(redis, arguments.key_prefix)
        await redis.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    summary = asyncio.run(run_benchmark(arguments))
    print(json.dumps(summary, sort_keys=True))
    return int(
        summary["returned_min"] < 20
        or summary["empty_count"] > 0
        or summary["p95_ms"] > 100
    )
