from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _require_bash() -> str:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to run the repository hygiene script")
    return bash


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _stage(path: Path, *files: str) -> None:
    subprocess.run(["git", "add", *files], cwd=path, check=True)


def _copy_hygiene_script(path: Path) -> None:
    source = REPO_ROOT / "scripts" / "check_repo_hygiene.sh"
    assert source.is_file()
    target = path / "scripts" / "check_repo_hygiene.sh"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def test_repo_hygiene_allows_regular_tracked_files(tmp_path: Path) -> None:
    bash = _require_bash()
    _init_git_repo(tmp_path)
    _copy_hygiene_script(tmp_path)
    _write(tmp_path / "README.md", "# Demo\n")
    _stage(tmp_path, "README.md")

    result = subprocess.run(
        [bash, "scripts/check_repo_hygiene.sh"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "[hygiene] OK: no forbidden tracked files." in result.stdout


def test_repo_hygiene_fails_for_generated_and_local_files(tmp_path: Path) -> None:
    bash = _require_bash()
    _init_git_repo(tmp_path)
    _copy_hygiene_script(tmp_path)
    _write(tmp_path / "AGENTS.md", "local instructions\n")
    _write(tmp_path / "data" / "eval_results.json", "{}\n")
    _stage(tmp_path, "AGENTS.md", "data/eval_results.json")

    result = subprocess.run(
        [bash, "scripts/check_repo_hygiene.sh"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "[hygiene] Forbidden tracked files detected:" in result.stdout
    assert "AGENTS.md" in result.stdout
    assert "data/eval_results.json" in result.stdout
