import json
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
