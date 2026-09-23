from ip_proxy_pool.cli import build_parser


def test_cli_has_all_required_roles() -> None:
    parser = build_parser()

    for command in ("api", "collect", "check", "all", "doctor", "peer-sync"):
        assert parser.parse_args([command]).command == command


def test_import_legacy_arguments_are_bounded_and_explicit() -> None:
    parser = build_parser()

    arguments = parser.parse_args(
        [
            "import-legacy",
            "--redis-key",
            "proxies",
            "--domain",
            "example.com",
            "--dry-run",
        ]
    )

    assert arguments.redis_key == "proxies"
    assert arguments.domain == "example.com"
    assert arguments.dry_run is True


def test_rebuild_latency_index_arguments_are_explicit() -> None:
    arguments = build_parser().parse_args(
        ["rebuild-latency-index", "--domain", "portal.daqihui.com", "--dry-run"]
    )

    assert arguments.command == "rebuild-latency-index"
    assert arguments.domain == "portal.daqihui.com"
    assert arguments.dry_run is True
