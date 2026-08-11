from ip_proxy_pool.storage.keys import keys_for


def test_domain_key_is_encoded_and_stable() -> None:
    keys = keys_for("ippool:test", "2001:4860:4860::8888")

    assert keys.records == "ippool:test:pool:2001%3A4860%3A4860%3A%3A8888:records"
    assert keys.quality.endswith(":quality")
    assert keys.due.endswith(":due")


def test_key_builder_rejects_empty_prefix_or_domain() -> None:
    for prefix, domain in (("", "example.com"), ("ippool:test", "")):
        try:
            keys_for(prefix, domain)
        except ValueError:
            continue
        raise AssertionError("empty key components must be rejected")
