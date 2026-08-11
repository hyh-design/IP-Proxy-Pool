import pytest

from ip_proxy_pool.domain import normalize_target_url, registrable_domain


def test_psl_domain_and_ipv6_literal() -> None:
    assert registrable_domain("https://service.example.co.za/a") == "example.co.za"
    assert registrable_domain("http://[2001:4860:4860::8888]:8080/") == "2001:4860:4860::8888"


def test_private_psl_keeps_github_tenants_separate() -> None:
    assert registrable_domain("https://tenant.github.io/path") == "tenant.github.io"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "https://user:secret@example.com/path",
        "example.com/no-scheme",
    ],
)
def test_target_url_rejects_unsafe_or_ambiguous_values(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_target_url(url)
