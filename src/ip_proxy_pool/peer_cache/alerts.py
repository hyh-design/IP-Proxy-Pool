"""Peer alerts with DingTalk business-level acknowledgement."""

from dataclasses import dataclass

import httpx


@dataclass(frozen=True, slots=True)
class PeerAlertEvent:
    event_id: str
    peer_name: str
    domain: str
    event_type: str
    status: str


class PeerAlertNotifier:
    def __init__(self, webhook_url: str, *, client: httpx.AsyncClient | None = None) -> None:
        if not webhook_url.startswith("https://"):
            raise ValueError("peer alert webhook requires HTTPS")
        self.webhook_url = webhook_url
        self.client = client or httpx.AsyncClient(
            follow_redirects=False, trust_env=False, timeout=3.0
        )
        self._owns_client = client is None

    async def send(self, event: PeerAlertEvent) -> bool:
        content = (
            f"代理缓存事件 {event.event_type} {event.status} "
            f"node={event.peer_name} domain={event.domain} event_id={event.event_id}"
        )
        try:
            response = await self.client.post(
                self.webhook_url,
                json={"msgtype": "text", "text": {"content": content}},
                follow_redirects=False,
            )
            if not response.is_success:
                return False
            body = response.json()
            return isinstance(body, dict) and body.get("errcode") == 0
        except Exception:
            return False

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
