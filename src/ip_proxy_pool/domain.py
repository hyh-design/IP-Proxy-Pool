"""URL normalization and Public Suffix List domain extraction."""

import ipaddress
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

import tldextract


@lru_cache(maxsize=1)
def _extractor() -> tldextract.TLDExtract:
    return tldextract.TLDExtract(
        suffix_list_urls=(),
        include_psl_private_domains=True,
    )


def normalize_target_url(value: str) -> str:
    """Return a fragment-free HTTP(S) URL or raise for ambiguous input."""
    raw = value.strip()
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("target URL must use http or https")
    if parsed.hostname is None:
        raise ValueError("target URL must contain a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("target URL userinfo is forbidden")

    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, parsed.query, ""))


def registrable_domain(host_or_url: str) -> str:
    """Return a normalized IP literal or PSL-aware registrable domain."""
    raw = host_or_url.strip()
    if not raw:
        raise ValueError("host or URL must not be empty")

    literal = raw.strip("[]")
    try:
        return str(ipaddress.ip_address(literal))
    except ValueError:
        pass

    if "://" in raw:
        host = urlsplit(normalize_target_url(raw)).hostname
    else:
        host = urlsplit(f"//{raw}").hostname
    if host is None:
        raise ValueError("unable to determine host")

    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass

    extracted = _extractor()(host.lower().rstrip("."))
    if extracted.domain and extracted.suffix:
        return f"{extracted.domain}.{extracted.suffix}"
    return host.lower().rstrip(".")
