import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]


@pytest.fixture(scope="module")
def compose_config() -> dict[str, Any]:
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def peer_compose_config(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    env_path = tmp_path_factory.mktemp("peer-compose") / "peer-sync.env"
    env_path.write_text(
        "IP_POOL_PEER_CACHE__ENABLED=true\nIP_POOL_PEER_CACHE__API_KEY=test-only-key\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["docker", "compose", "--profile", "peer-cache", "config", "--format", "json"],
        cwd=ROOT,
        env={**os.environ, "IP_POOL_PEER_SYNC_ENV_FILE": str(env_path)},
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_compose_does_not_publish_redis(compose_config: dict[str, Any]) -> None:
    assert "ports" not in compose_config["services"]["redis"]


def test_api_binds_only_to_loopback(compose_config: dict[str, Any]) -> None:
    ports = compose_config["services"]["api"]["ports"]

    assert len(ports) == 1
    assert ports[0]["host_ip"] == "127.0.0.1"
    assert ports[0]["published"] == "8000"
    assert ports[0]["target"] == 8000


def test_dashboard_does_not_add_compose_service_or_port(
    compose_config: dict[str, Any],
) -> None:
    assert set(compose_config["services"]) == {"redis", "api", "collector", "checker"}
    assert compose_config["services"]["api"]["ports"] == [
        {
            "mode": "ingress",
            "target": 8000,
            "published": "8000",
            "protocol": "tcp",
            "host_ip": "127.0.0.1",
        }
    ]


def test_only_network_workers_receive_external_egress(
    compose_config: dict[str, Any],
) -> None:
    services = compose_config["services"]

    assert set(services["redis"]["networks"]) == {"internal"}
    assert set(services["api"]["networks"]) == {"internal", "edge"}
    assert set(services["collector"]["networks"]) == {"internal", "edge"}
    assert set(services["checker"]["networks"]) == {"internal", "edge"}


def test_roles_share_image_and_wait_for_healthy_redis(
    compose_config: dict[str, Any],
) -> None:
    services = compose_config["services"]

    assert {services[name]["image"] for name in ("api", "collector", "checker")} == {
        "ip-proxy-pool:local"
    }
    for name in ("api", "collector", "checker"):
        assert services[name]["depends_on"]["redis"]["condition"] == "service_healthy"


def test_container_security_defaults_are_enabled(
    compose_config: dict[str, Any],
) -> None:
    for name in ("api", "collector", "checker"):
        service = compose_config["services"][name]
        assert service["read_only"] is True
        assert service["security_opt"] == ["no-new-privileges:true"]


def test_peer_profile_has_isolated_services_and_no_new_public_ports(
    peer_compose_config: dict[str, Any],
) -> None:
    services = peer_compose_config["services"]
    assert set(services) == {"redis", "api", "collector", "checker", "peer-tunnel", "peer-sync"}
    tunnel = services["peer-tunnel"]
    sync = services["peer-sync"]
    assert tunnel["entrypoint"] == ["/usr/bin/ssh"]
    assert tunnel["user"] == "0:0"
    assert tunnel["read_only"] is True
    assert tunnel["cap_drop"] == ["ALL"]
    assert tunnel["security_opt"] == ["no-new-privileges:true"]
    assert set(tunnel["networks"]) == {"internal", "edge"}
    assert set(sync["networks"]) == {"internal"}
    command = tunnel["command"]
    assert command[:2] == ["-N", "-T"]
    assert command[command.index("-L") + 1] == "0.0.0.0:8000:127.0.0.1:8000"
    assert command[-1] == "peer-export"
    for option in (
        "BatchMode=yes",
        "IdentitiesOnly=yes",
        "StrictHostKeyChecking=yes",
        "ExitOnForwardFailure=yes",
        "ServerAliveInterval=15",
        "ServerAliveCountMax=3",
    ):
        assert option in command
    assert "env_file" not in tunnel
    assert len(tunnel["volumes"]) == 3
    assert all(volume["read_only"] is True for volume in tunnel["volumes"])
    assert "health/live" in tunnel["healthcheck"]["test"][-1]
    assert "heartbeat_at" in sync["healthcheck"]["test"][-1]
    assert "ports" not in tunnel
    assert "ports" not in sync
    assert "IP_POOL_PEER_CACHE__API_KEY" not in tunnel.get("environment", {})
    for name in ("collector", "checker"):
        assert "IP_POOL_PEER_CACHE__API_KEY" not in services[name]["environment"]
        assert "IP_POOL_PEER_ALERTS__WEBHOOK_URL" not in services[name]["environment"]


def test_peer_profile_requires_sync_role_env_file(tmp_path: Path) -> None:
    result = subprocess.run(
        ["docker", "compose", "--profile", "peer-cache", "config", "--quiet"],
        cwd=ROOT,
        env={**os.environ, "IP_POOL_PEER_SYNC_ENV_FILE": str(tmp_path / "missing.env")},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "env file" in result.stderr.lower()
