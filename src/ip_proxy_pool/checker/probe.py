import asyncio
import re
from collections.abc import Sequence
from time import perf_counter
from typing import Protocol

import httpx

from ip_proxy_pool.checker.errors import ProbeCategory, ProbeResult
from ip_proxy_pool.models import ProxyEndpoint, TestTarget

_CREDENTIAL_URL = re.compile(r"(https?://)[^/@\s]+@", re.IGNORECASE)


class ProbeClient(Protocol):
    async def request(
        self, endpoint: ProxyEndpoint | None, target: TestTarget
    ) -> httpx.Response: ...


class HttpxProbeClient:
    """Production probe client with environment proxies and redirects disabled."""

    async def request(self, endpoint: ProxyEndpoint | None, target: TestTarget) -> httpx.Response:
        proxy = f"http://{endpoint.canonical}" if endpoint is not None else None
        async with httpx.AsyncClient(
            proxy=proxy,
            timeout=target.timeout_seconds,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            return await client.get(str(target.url))


def _safe_message(error: BaseException | str) -> str:
    message = str(error)
    redacted = _CREDENTIAL_URL.sub(r"\1***@", message)
    return redacted[:256]


def _failure(
    category: ProbeCategory,
    error_type: str,
    message: str,
    *,
    status_code: int | None = None,
) -> ProbeResult:
    return ProbeResult(
        category=category,
        status_code=status_code,
        error_type=error_type,
        error_message=_safe_message(message),
    )


def _validate_response(
    response: httpx.Response,
    target: TestTarget,
    *,
    failure_category: ProbeCategory,
    latency_ms: float,
    baseline: bool,
) -> ProbeResult:
    if baseline and response.status_code >= 500:
        return _failure(
            ProbeCategory.SYSTEM_ERROR,
            "unexpected_status",
            f"target returned HTTP {response.status_code}",
            status_code=response.status_code,
        )
    if response.status_code not in target.expected_statuses:
        return _failure(
            failure_category,
            "unexpected_status",
            f"expected {sorted(target.expected_statuses)}, got {response.status_code}",
            status_code=response.status_code,
        )
    if target.expected_text is not None and target.expected_text not in response.text:
        return _failure(
            failure_category,
            "response_mismatch",
            "expected text was not present",
            status_code=response.status_code,
        )
    if target.json_keys:
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or any(key not in body for key in target.json_keys):
            return _failure(
                failure_category,
                "response_mismatch",
                "expected JSON keys were not present",
                status_code=response.status_code,
            )
    return ProbeResult(
        category=ProbeCategory.SUCCESS,
        latency_ms=latency_ms,
        status_code=response.status_code,
    )


async def _probe(
    endpoint: ProxyEndpoint | None,
    target: TestTarget,
    *,
    client: ProbeClient,
) -> ProbeResult:
    started = perf_counter()
    try:
        response = await client.request(endpoint, target)
    except asyncio.CancelledError as error:
        return _failure(ProbeCategory.CANCELLED, "cancelled", _safe_message(error))
    except httpx.TransportError as error:
        category = ProbeCategory.PROXY_ERROR if endpoint is not None else ProbeCategory.SYSTEM_ERROR
        return _failure(category, type(error).__name__, _safe_message(error))
    except Exception as error:
        return _failure(ProbeCategory.SYSTEM_ERROR, type(error).__name__, _safe_message(error))

    latency_ms = (perf_counter() - started) * 1000
    return _validate_response(
        response,
        target,
        failure_category=(
            ProbeCategory.PROXY_ERROR if endpoint is not None else ProbeCategory.SYSTEM_ERROR
        ),
        latency_ms=latency_ms,
        baseline=endpoint is None,
    )


async def probe_proxy(
    endpoint: ProxyEndpoint,
    target: TestTarget,
    *,
    client: ProbeClient,
) -> ProbeResult:
    return await _probe(endpoint, target, client=client)


async def probe_proxy_targets(
    endpoint: ProxyEndpoint,
    targets: Sequence[TestTarget],
    *,
    client: ProbeClient,
) -> ProbeResult:
    """Require every validation target to succeed in one scoring round."""
    if not targets:
        raise ValueError("at least one proxy validation target is required")
    maximum_latency = 0.0
    status_code: int | None = None
    for target in targets:
        result = await probe_proxy(endpoint, target, client=client)
        if result.category is not ProbeCategory.SUCCESS:
            return result
        if result.latency_ms is not None:
            maximum_latency = max(maximum_latency, result.latency_ms)
        status_code = result.status_code
    return ProbeResult(
        category=ProbeCategory.SUCCESS,
        latency_ms=maximum_latency,
        status_code=status_code,
    )


async def probe_target_baseline(
    target: TestTarget,
    *,
    client: ProbeClient,
) -> ProbeResult:
    return await _probe(None, target, client=client)
