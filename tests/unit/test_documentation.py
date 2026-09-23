import re
from pathlib import Path

from ip_proxy_pool.cli import build_parser

ROOT = Path(__file__).parents[2]


def cli_commands() -> set[str]:
    parser = build_parser()
    subparsers = next(
        action for action in parser._actions if hasattr(action, "choices") and action.choices
    )
    return set(subparsers.choices)


def test_readme_commands_exist() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for command in (
        "api",
        "collect",
        "check",
        "all",
        "doctor",
        "import-legacy",
        "rebuild-latency-index",
        "peer-sync",
    ):
        assert command in cli_commands()
        assert f"ip-pool {command}" in readme


def test_documented_env_keys_match_example() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    configuration = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    example_keys = {
        line.split("=", maxsplit=1)[0]
        for line in example.splitlines()
        if line.startswith("IP_POOL_")
    }
    documented_keys = set(re.findall(r"`(IP_POOL_[A-Z0-9_]+)`", configuration))

    assert example_keys == documented_keys


def test_operator_documents_and_ci_gates_exist() -> None:
    for name in (
        "architecture.md",
        "security.md",
        "operations.md",
        "migration.md",
        "configuration.md",
    ):
        assert (ROOT / "docs" / name).stat().st_size > 200

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for gate in (
        "ruff format --check",
        "ruff check",
        "mypy src",
        "pytest",
        "docker build",
        "docker compose config",
        "pip-audit",
    ):
        assert gate in workflow


def test_dashboard_docs_describe_ranges_auth_and_internal_access() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    security = (ROOT / "docs" / "security.md").read_text(encoding="utf-8")
    operations = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")

    assert "/dashboard" in readme
    assert "1h / 6h / 12h / 24h / 7d / 30d" in readme
    assert "sessionStorage" in security
    assert "SSH 隧道" in operations
