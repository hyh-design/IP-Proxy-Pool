import pytest

from ip_proxy_pool.models import ProxyEndpoint
from ip_proxy_pool.security.network import (
    NetworkBoundaryError,
    validate_proxy_endpoint,
    validate_public_url,
)


@pytest.mark.asyncio
async def test_url_rejects_hostname_resolving_to_private_ip() -> None:
    async def resolver(host: str) -> set[str]:
        assert host == "allowed.example"
        return {"127.0.0.1"}

    with pytest.raises(NetworkBoundaryError, match="non-global"):
        await validate_public_url(
            "https://allowed.example/path",
            allowed_hosts={"allowed.example"},
            resolver=resolver,
        )


@pytest.mark.asyncio
async def test_url_rejects_mixed_public_and_private_dns_answers() -> None:
    async def resolver(_: str) -> set[str]:
        return {"1.1.1.1", "10.0.0.1"}

    with pytest.raises(NetworkBoundaryError, match="non-global"):
        await validate_public_url(
            "https://allowed.example/",
            allowed_hosts={"allowed.example"},
            resolver=resolver,
        )


@pytest.mark.asyncio
async def test_url_accepts_exact_allowlisted_public_host() -> None:
    async def resolver(_: str) -> set[str]:
        return {"1.1.1.1", "2606:4700:4700::1111"}

    validated = await validate_public_url(
        "https://allowed.example/path#fragment",
        allowed_hosts={"allowed.example"},
        resolver=resolver,
    )

    assert validated.url == "https://allowed.example/path"
    assert {str(address) for address in validated.addresses} == {
        "1.1.1.1",
        "2606:4700:4700::1111",
    }


def test_proxy_rejects_non_global_address_by_default() -> None:
    with pytest.raises(NetworkBoundaryError, match="non-global"):
        validate_proxy_endpoint(ProxyEndpoint.parse("127.0.0.1:8080"))


def test_proxy_development_override_allows_non_global_address() -> None:
    endpoint = ProxyEndpoint.parse("127.0.0.1:8080")

    assert validate_proxy_endpoint(endpoint, allow_non_global=True) == endpoint


def test_proxy_rejects_explicitly_blocked_network() -> None:
    endpoint = ProxyEndpoint.parse("104.26.15.61:80")

    with pytest.raises(NetworkBoundaryError, match="blocked network"):
        validate_proxy_endpoint(endpoint, blocked_networks=("104.24.0.0/14",))
