import httpx
import pytest

from ip_proxy_pool.checker.errors import ProbeCategory, ProbeResult
from ip_proxy_pool.checker.probe import probe_proxy, probe_target_baseline
from ip_proxy_pool.models import ProxyEndpoint
from ip_proxy_pool.models import TestTarget as Target


class StubProbeClient:
    def __init__(
        self,
        *,
        response: httpx.Response | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.response = response or httpx.Response(200, json={"origin": "1.1.1.1"})
        self.error = error

    async def request(self, endpoint: ProxyEndpoint | None, target: Target) -> httpx.Response:
        del endpoint, target
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture
def target() -> Target:
    return Target.model_validate(
        {
            "name": "httpbin",
            "url": "https://httpbin.org/ip",
            "domain": "httpbin.org",
            "expected_statuses": {200},
            "json_keys": ("origin",),
        }
    )


@pytest.fixture
def endpoint() -> ProxyEndpoint:
    return ProxyEndpoint.parse("1.1.1.1:80")


async def test_proxy_connect_error_is_proxy_failure(
    target: Target, endpoint: ProxyEndpoint
) -> None:
    client = StubProbeClient(error=httpx.ProxyError("connect failed"))

    result = await probe_proxy(endpoint, target, client=client)

    assert result.category is ProbeCategory.PROXY_ERROR


async def test_proxy_connect_timeout_is_proxy_failure(
    target: Target, endpoint: ProxyEndpoint
) -> None:
    client = StubProbeClient(error=httpx.ConnectTimeout("proxy timeout"))

    result = await probe_proxy(endpoint, target, client=client)

    assert result.category is ProbeCategory.PROXY_ERROR


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadError("read failed"),
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectError("connect failed"),
        httpx.RemoteProtocolError("protocol failed"),
    ],
)
async def test_proxy_transport_errors_are_proxy_failures(
    target: Target,
    endpoint: ProxyEndpoint,
    error: httpx.TransportError,
) -> None:
    result = await probe_proxy(endpoint, target, client=StubProbeClient(error=error))

    assert result.category is ProbeCategory.PROXY_ERROR


async def test_unknown_exception_is_system_error(target: Target, endpoint: ProxyEndpoint) -> None:
    client = StubProbeClient(error=RuntimeError("bug"))

    result = await probe_proxy(endpoint, target, client=client)

    assert result.category is ProbeCategory.SYSTEM_ERROR


async def test_expected_status_content_and_json_are_required(
    target: Target, endpoint: ProxyEndpoint
) -> None:
    status = await probe_proxy(
        endpoint,
        target,
        client=StubProbeClient(response=httpx.Response(403, json={"origin": "x"})),
    )
    missing_json = await probe_proxy(
        endpoint,
        target,
        client=StubProbeClient(response=httpx.Response(200, json={"wrong": "x"})),
    )
    text_target = target.model_copy(update={"expected_text": "needle", "json_keys": ()})
    missing_text = await probe_proxy(
        endpoint,
        text_target,
        client=StubProbeClient(response=httpx.Response(200, text="haystack")),
    )

    assert status.category is ProbeCategory.PROXY_ERROR
    assert status.status_code == 403
    assert missing_json.error_type == "response_mismatch"
    assert missing_text.error_type == "response_mismatch"


async def test_valid_response_succeeds(target: Target, endpoint: ProxyEndpoint) -> None:
    result = await probe_proxy(endpoint, target, client=StubProbeClient())

    assert result.category is ProbeCategory.SUCCESS
    assert result.status_code == 200
    assert result.latency_ms is not None
    assert result.latency_ms >= 0


async def test_baseline_5xx_is_a_system_error(target: Target) -> None:
    result = await probe_target_baseline(
        target,
        client=StubProbeClient(response=httpx.Response(503, text="unavailable")),
    )

    assert result.category is ProbeCategory.SYSTEM_ERROR
    assert result.error_type == "unexpected_status"


async def test_errors_are_redacted_and_truncated(target: Target, endpoint: ProxyEndpoint) -> None:
    secret = "https://user:password@proxy.example/" + "x" * 400

    result = await probe_proxy(endpoint, target, client=StubProbeClient(error=RuntimeError(secret)))

    assert result.error_message is not None
    assert "password" not in result.error_message
    assert "user" not in result.error_message
    assert "***@proxy.example" in result.error_message
    assert len(result.error_message) <= 256


def test_probe_result_rejects_oversized_error_messages() -> None:
    with pytest.raises(ValueError):
        ProbeResult(
            category=ProbeCategory.SYSTEM_ERROR,
            error_message="x" * 257,
        )
