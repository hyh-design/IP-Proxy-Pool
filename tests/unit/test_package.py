import subprocess
import zipfile
from pathlib import Path

from ip_proxy_pool import __version__

ROOT = Path(__file__).parents[2]


def test_package_exposes_version() -> None:
    assert __version__ == "0.1.0"


def test_wheel_contains_dashboard_assets(tmp_path: Path) -> None:
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("*.whl"))
    required = {
        "index.html",
        "dashboard.css",
        "api.js",
        "charts.js",
        "dashboard.js",
        "fonts/IBMPlexSans-Regular.woff2",
        "fonts/IBMPlexSans-SemiBold.woff2",
        "fonts/IBMPlexMono-Medium.woff2",
        "fonts/OFL.txt",
    }
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())

    for relative in required:
        assert f"ip_proxy_pool/static/dashboard/{relative}" in names
