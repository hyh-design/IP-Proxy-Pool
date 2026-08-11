"""DNS-aware controls that prevent unsafe proxy and target addresses."""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Collection
from typing import NamedTuple
from urllib.parse import urlsplit

from ip_proxy_pool.domain import normalize_target_url
from ip_proxy_pool.models import ProxyEndpoint


class NetworkBoundaryError(ValueError):
    """Raised when a proxy or target crosses the configured network boundary."""


IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str], Awaitable[set[str]]]

CLOUDFLARE_IPV4_NETWORKS: tuple[str, ...] = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
)


class ValidatedURL(NamedTuple):
    url: str
    host: str
    addresses: frozenset[IPAddress]


def is_global_address(address: IPAddress) -> bool:
    return address.is_global


def validate_proxy_endpoint(
    endpoint: ProxyEndpoint,
    *,
    allow_non_global: bool = False,
    blocked_networks: Collection[str | IPNetwork] = (),
) -> ProxyEndpoint:
    if not allow_non_global and not is_global_address(endpoint.host):
        raise NetworkBoundaryError("proxy endpoint uses a non-global address")
    for raw_network in blocked_networks:
        try:
            network = (
                raw_network
                if isinstance(raw_network, (ipaddress.IPv4Network, ipaddress.IPv6Network))
                else ipaddress.ip_network(raw_network, strict=True)
            )
        except ValueError as error:
            raise NetworkBoundaryError("blocked proxy network is invalid") from error
        if endpoint.host.version == network.version and endpoint.host in network:
            raise NetworkBoundaryError("proxy endpoint uses a blocked network")
    return endpoint


def compile_proxy_networks(networks: Collection[str]) -> tuple[IPNetwork, ...]:
    try:
        return tuple(ipaddress.ip_network(network, strict=True) for network in networks)
    except ValueError as error:
        raise NetworkBoundaryError("blocked proxy network is invalid") from error


async def system_resolver(host: str) -> set[str]:
    loop = asyncio.get_running_loop()
    answers = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return {answer[4][0] for answer in answers}


async def validate_public_url(
    url: str,
    *,
    allowed_hosts: Collection[str],
    resolver: Resolver = system_resolver,
) -> ValidatedURL:
    normalized = normalize_target_url(url)
    parsed = urlsplit(normalized)
    host = parsed.hostname
    if host is None:
        raise NetworkBoundaryError("target URL has no host")

    normalized_host = host.lower().rstrip(".")
    normalized_allowlist = {item.lower().rstrip(".") for item in allowed_hosts}
    if not normalized_allowlist or normalized_host not in normalized_allowlist:
        raise NetworkBoundaryError("target host is not allowlisted")

    try:
        raw_addresses = {str(ipaddress.ip_address(normalized_host))}
    except ValueError:
        try:
            raw_addresses = await resolver(normalized_host)
        except (OSError, socket.gaierror) as exc:
            raise NetworkBoundaryError("target host could not be resolved") from exc

    try:
        addresses = frozenset(ipaddress.ip_address(value) for value in raw_addresses)
    except ValueError as exc:
        raise NetworkBoundaryError("resolver returned an invalid address") from exc

    if not addresses or any(not is_global_address(address) for address in addresses):
        raise NetworkBoundaryError("target resolves to a non-global address")
    return ValidatedURL(normalized, normalized_host, addresses)
