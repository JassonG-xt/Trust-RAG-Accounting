from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_deploy_readiness_script_passes_in_repo_root() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to run the deploy readiness script")

    script = REPO_ROOT / "scripts" / "check_deploy_readiness.sh"
    assert script.is_file()

    result = subprocess.run(
        [bash, "scripts/check_deploy_readiness.sh"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "[deploy-readiness] OK" in result.stdout
