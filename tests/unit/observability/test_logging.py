from ip_proxy_pool.observability.logging import redact_secrets


def test_log_processor_redacts_nested_secrets() -> None:
    event = redact_secrets(
        None,
        None,
        {
            "headers": {
                "X-API-Key": "secret",
                "Authorization": "Bearer token",
            },
            "redis_url": "redis://:pw@host/0",
            "nested": {"password": "pw2"},
        },
    )

    rendered = repr(event)
    assert "secret" not in rendered
    assert "Bearer token" not in rendered
    assert "pw" not in rendered


def test_error_text_is_truncated() -> None:
    event = redact_secrets(None, None, {"error": "x" * 500})

    assert len(event["error"]) == 256
