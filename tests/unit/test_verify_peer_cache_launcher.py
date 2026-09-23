"""The host-side verification launcher must support a compatible Python."""

import os
import subprocess
import sys
from pathlib import Path


def test_launcher_uses_selected_python_when_system_python_is_incompatible(
    tmp_path: Path,
) -> None:
    old_python = tmp_path / "python3"
    old_python.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
    old_python.chmod(0o755)
    script = Path(__file__).resolve().parents[2] / "scripts" / "verify-peer-cache.sh"

    result = subprocess.run(
        ["sh", str(script), "--help"],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "PYTHON": sys.executable},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout
