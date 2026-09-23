from pathlib import Path

import pytest

from ip_proxy_pool.config import Settings


def test_default_validation_target_uses_domestic_ip_service() -> None:
    target = Settings(_env_file=None).target.to_validation_targets()[0]

    assert target.name == "ipip"
    assert str(target.url) == "https://myip.ipip.net/json"
    assert target.expected_statuses == frozenset({200})
    assert target.json_keys == ("data",)


def test_secure_defaults_do_not_read_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.example").write_text("IP_POOL_API__HOST=0.0.0.0\n", encoding="utf-8")

    settings = Settings(_env_file=None)

    assert settings.api.host == "127.0.0.1"
    assert settings.api.auth_enabled is True
    assert settings.api.max_page_size == 200
    assert settings.selection.min_score == 90
    assert settings.selection.max_latency_ms == 5000
    assert settings.selection.max_checked_age_seconds == 600
    assert settings.selection.min_consecutive_successes == 2
    assert settings.checker.concurrency == 100
    assert settings.checker.batch_size == 200
    assert settings.checker.drop_after_failures == 5
    assert settings.collector.max_pages_per_source == 5
    assert settings.collector.max_proxies_per_source_round == 2000
    assert settings.collector.max_pool_size_per_domain == 10000
    assert settings.collector.collection_interval_seconds == 300
    assert settings.collector.inventory_check_interval_seconds == 60
    assert settings.collector.low_inventory_threshold == 20
    assert settings.collector.low_inventory_min_score == 80
    assert settings.collector.low_inventory_max_latency_ms == 2000
    assert settings.collector.low_inventory_max_new_candidates == 500
    assert "104.16.0.0/13" in settings.security.blocked_proxy_networks
    assert settings.target.validation_targets[0].name == "ipip"
    assert str(settings.target.validation_targets[0].url) == "https://myip.ipip.net/json"


def test_nested_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IP_POOL_CHECKER__CONCURRENCY", "64")

    settings = Settings(_env_file=None)

    assert settings.checker.concurrency == 64


def test_target_environment_override_builds_daqihui_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IP_POOL_TARGET__NAME", "daqihui")
    monkeypatch.setenv("IP_POOL_TARGET__URL", "https://portal.daqihui.com/")
    monkeypatch.setenv("IP_POOL_TARGET__DOMAIN", "portal.daqihui.com")
    monkeypatch.setenv("IP_POOL_TARGET__EXPECTED_STATUSES", "[200]")
    monkeypatch.setenv("IP_POOL_TARGET__JSON_KEYS", "[]")
    monkeypatch.setenv("IP_POOL_TARGET__TIMEOUT_SECONDS", "5")

    target = Settings(_env_file=None).target.to_test_target()

    assert target.name == "daqihui"
    assert str(target.url) == "https://portal.daqihui.com/"
    assert target.domain == "portal.daqihui.com"
    assert target.expected_statuses == frozenset({200})
    assert target.json_keys == ()
    assert target.timeout_seconds == 5


def test_api_startup_requires_keys_and_cursor_secret() -> None:
    settings = Settings(_env_file=None)

    with pytest.raises(ValueError, match="API key"):
        settings.validate_api_startup()


def test_admin_probe_requires_admin_key_and_allowlist() -> None:
    settings = Settings.model_validate(
        {
            "api": {
                "api_keys": ["read-key"],
                "cursor_secret": "x" * 32,
                "admin_probe_enabled": True,
            }
        }
    )

    with pytest.raises(ValueError, match="admin API key"):
        settings.validate_api_startup()


def test_api_startup_rejects_duplicate_keys() -> None:
    settings = Settings.model_validate(
        {
            "api": {
                "api_keys": ["duplicate"],
                "admin_api_keys": ["duplicate"],
                "cursor_secret": "x" * 32,
            }
        }
    )

    with pytest.raises(ValueError, match="unique"):
        settings.validate_api_startup()


def test_disabled_auth_does_not_require_api_keys() -> None:
    settings = Settings.model_validate(
        {
            "api": {
                "auth_enabled": False,
                "cursor_secret": "x" * 32,
            }
        }
    )

    settings.validate_api_startup()


def test_dashboard_defaults_are_bounded() -> None:
    value = Settings(_env_file=None).dashboard

    assert value.enabled is True
    assert value.snapshot_interval_seconds == 300
    assert value.short_retention_hours == 48
    assert value.long_retention_hours == 720
    assert value.heartbeat_interval_seconds == 30
    assert value.heartbeat_ttl_seconds == 90
    assert value.refresh_seconds == 30
    assert value.max_aggregate_records == 20_000


def test_quota_keys_must_be_distinct_from_business_roles() -> None:
    settings = Settings.model_validate(
        {
            "api": {"api_keys": ["shared"], "cursor_secret": "x" * 32},
            "reclaim_quota": {"enabled": True, "api_keys": ["shared", "quota-two"]},
        }
    )

    with pytest.raises(ValueError, match="unique"):
        settings.validate_api_startup()


def test_quota_authority_requires_authentication_and_two_keys() -> None:
    no_auth = Settings.model_validate(
        {
            "api": {"auth_enabled": False, "cursor_secret": "x" * 32},
            "reclaim_quota": {"enabled": True, "api_keys": ["one", "two"]},
        }
    )
    with pytest.raises(ValueError, match="authentication"):
        no_auth.validate_api_startup()

    one_key = Settings.model_validate(
        {
            "api": {"api_keys": ["read"], "cursor_secret": "x" * 32},
            "reclaim_quota": {"enabled": True, "api_keys": ["one"]},
        }
    )
    with pytest.raises(ValueError, match="two dedicated"):
        one_key.validate_api_startup()


def test_peer_export_requires_scoped_key_node_and_authentication() -> None:
    base = {"api": {"api_keys": ["read"], "cursor_secret": "x" * 32}}
    for peer, pattern in (
        ({"enabled": True, "node_id": "system-one"}, "dedicated client key"),
        ({"enabled": True, "api_keys": ["peer"]}, "node_id"),
        ({"enabled": True, "node_id": "system-one", "api_keys": ["read"]}, "unique"),
    ):
        with pytest.raises(ValueError, match=pattern):
            Settings.model_validate({**base, "peer_export": peer}).validate_api_startup()
    with pytest.raises(ValueError, match="authentication"):
        Settings.model_validate(
            {
                "api": {"auth_enabled": False, "cursor_secret": "x" * 32},
                "peer_export": {"enabled": True, "node_id": "system-one", "api_keys": ["peer"]},
            }
        ).validate_api_startup()


def test_api_peer_cache_does_not_require_sync_secret_but_worker_does() -> None:
    settings = Settings.model_validate(
        {
            "api": {"api_keys": ["read"], "cursor_secret": "x" * 32},
            "peer_export": {"node_id": "system-one"},
            "peer_cache": {"enabled": True, "peer_name": "system-two", "origin_node": "system-two"},
            "peer_alerts": {"enabled": True, "webhook_url": "https://example.invalid/hook"},
        }
    )
    settings.validate_api_startup()
    with pytest.raises(ValueError, match="base_url and API key"):
        settings.validate_peer_sync_startup()


def test_ownership_requires_distinct_account_keys_on_both_systems() -> None:
    settings = Settings.model_validate(
        {
            "api": {"api_keys": ["read"], "cursor_secret": "x" * 32},
            "ownership": {
                "enabled": True,
                "members": {
                    "one:a": {"system_id": "system-one", "api_key": "own-a"},
                    "two:b": {"system_id": "system-two", "api_key": "own-b"},
                },
            },
        }
    )

    settings.validate_api_startup()
    assert settings.ownership.members["two:b"].system_id == "system-two"


@pytest.mark.parametrize("shared_role", ["normal", "admin", "quota", "peer", "ownership"])
def test_ownership_rejects_key_reused_by_any_role(shared_role: str) -> None:
    api = {"api_keys": ["read"], "cursor_secret": "x" * 32}
    quota = {}
    peer = {}
    ownership = {
        "enabled": True,
        "members": {
            "one:a": {"system_id": "system-one", "api_key": "own-a"},
            "two:b": {"system_id": "system-two", "api_key": "own-b"},
        },
    }
    if shared_role == "normal":
        api["api_keys"] = ["own-a"]
    elif shared_role == "admin":
        api["admin_api_keys"] = ["own-a"]
    elif shared_role == "quota":
        quota["api_keys"] = ["own-a"]
    elif shared_role == "peer":
        peer["api_keys"] = ["own-a"]
    else:
        ownership["members"]["two:b"]["api_key"] = "own-a"

    settings = Settings.model_validate(
        {"api": api, "reclaim_quota": quota, "peer_export": peer, "ownership": ownership}
    )
    with pytest.raises(ValueError, match="unique"):
        settings.validate_api_startup()


def test_ownership_rejects_incomplete_roster_and_disabled_auth() -> None:
    payload = {
        "api": {"api_keys": ["read"], "cursor_secret": "x" * 32},
        "ownership": {
            "enabled": True,
            "members": {"one:a": {"system_id": "system-one", "api_key": "own-a"}},
        },
    }
    with pytest.raises(ValueError, match="system-two"):
        Settings.model_validate(payload).validate_api_startup()

    payload["api"]["auth_enabled"] = False
    with pytest.raises(ValueError, match="authentication"):
        Settings.model_validate(payload).validate_api_startup()
