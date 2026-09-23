import httpx

from ip_proxy_pool.peer_cache.alerts import PeerAlertEvent, PeerAlertNotifier


async def test_webhook_requires_http_success_and_business_errcode_zero() -> None:
    responses = [
        httpx.Response(200, json={"errcode": 1}),
        httpx.Response(503, json={"errcode": 0}),
        httpx.Response(200, json={"errcode": 0}),
    ]
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: responses.pop(0)))
    try:
        notifier = PeerAlertNotifier("https://example.invalid/hook", client=client)
        event = PeerAlertEvent(
            "event-1", "system-two", "portal.daqihui.com", "sync_failure", "failure"
        )
        assert await notifier.send(event) is False
        assert await notifier.send(event) is False
        assert await notifier.send(event) is True
    finally:
        await client.aclose()
