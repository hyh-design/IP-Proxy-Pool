from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from redis.asyncio import Redis

from ip_proxy_pool.api.app import create_app
from ip_proxy_pool.config import Settings
from ip_proxy_pool.models import ProxyEndpoint, ProxyRecord, ProxyState
from ip_proxy_pool.peer_cache.models import PeerCacheRecord
from ip_proxy_pool.peer_cache.store import PeerCacheStore
from ip_proxy_pool.storage.keys import keys_for
from ip_proxy_pool.storage.repository import RedisRepository


async def test_selection_feedback_roundtrip_never_writes_formal_record_for_peer(
    isolated_redis: str,
) -> None:
    domain = "portal.daqihui.com"
    settings = Settings.model_validate(
        {
            "redis": {"url": isolated_redis, "key_prefix": "integration"},
            "api": {"api_keys": ["read"], "cursor_secret": "x" * 32},
            "target": {"domain": domain},
            "peer_export": {"node_id": "system-one"},
            "peer_cache": {
                "enabled": True,
                "peer_name": "system-two",
                "origin_node": "system-two",
                "base_url": "http://127.0.0.1:18000",
                "api_key": "remote-peer-key",
            },
            "dashboard": {"enabled": False},
        }
    )
    redis = Redis.from_url(isolated_redis, decode_responses=True)
    try:
        now = datetime.now(UTC)
        repository = RedisRepository(redis, prefix="integration")
        formal = ProxyRecord(
            endpoint=ProxyEndpoint.parse("1.1.1.1:80"),
            domain=domain,
            score=95,
            state=ProxyState.AVAILABLE,
            source_names={"formal"},
            first_seen_at=now,
            last_seen_at=now,
            last_checked_at=now,
            next_check_at=now + timedelta(minutes=5),
            latency_ewma_ms=100,
            consecutive_successes=3,
        )
        await repository.save_record(formal)
        await redis.set(keys_for("integration", domain).available_latency_ready, "1")
        cache = PeerCacheStore(
            redis,
            prefix="integration",
            peer_name="system-two",
            domain=domain,
            origin_node="system-two",
        )
        generation = await cache.begin_sync("sync")
        peer = PeerCacheRecord(
            endpoint="8.8.8.8:80",
            domain=domain,
            score=95,
            latency_ewma_ms=100,
            last_checked_at=now,
            consecutive_successes=3,
            source_names=("remote-formal",),
            peer_name="system-two",
            origin_node="system-two",
            synced_at=now,
            expires_at=now + timedelta(seconds=180),
        )
        assert (await cache.replace((peer,), generation, "sync", now)).accepted == 1
        before = await redis.hget(keys_for("integration", domain).records, "1.1.1.1:80")
        with TestClient(create_app(settings)) as client:
            response = client.get(
                "/v1/proxies/random",
                params={"domain": domain, "count": 2, "include_peer_cache": True},
                headers={"X-API-Key": "read"},
            )
            assert response.status_code == 200
            selected = response.json()["items"]
            assert [item["selection_source"] for item in selected] == ["formal", "peer"]
            assert selected[1]["selection_token"]
            feedback = client.post(
                "/v1/proxies/feedback",
                headers={"X-API-Key": "read"},
                json={
                    "domain": domain,
                    "endpoint": selected[1]["endpoint"],
                    "outcome": "proxy_error",
                    "selection_token": selected[1]["selection_token"],
                },
            )
            assert feedback.status_code == 200
            assert feedback.json()["selection_source"] == "peer"
            assert await redis.hget(keys_for("integration", domain).records, "1.1.1.1:80") == before
    finally:
        await redis.aclose()
