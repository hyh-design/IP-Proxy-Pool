import subprocess


def test_local_artifacts_are_not_tracked() -> None:
    tracked = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
    forbidden = [
        path
        for path in tracked
        if path.startswith(".codegraph/")
        or path.startswith("docs/superpowers/")
        or path == ".env"
        or (path.startswith(".env.") and path != ".env.example")
    ]

    assert forbidden == []


def test_ignore_rules_cover_local_and_generated_artifacts() -> None:
    targets = (
        ".codegraph/",
        "docs/superpowers/",
    )

    for target in targets:
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", target],
            check=False,
        )
        assert result.returncode == 0, target
