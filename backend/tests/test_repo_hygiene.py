from __future__ import annotations

import shutil
import subprocess
import tomllib
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


def test_dependency_compatibility_bounds_are_declared() -> None:
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]
    runtime = project["dependencies"]
    dev = project["optional-dependencies"]["dev"]

    assert any(item.startswith("fastapi") and "<0.136" in item for item in runtime)
    assert any(item.startswith("starlette") and "<1.0" in item for item in runtime)
    assert any(item.startswith("anyio") and "<4.13" in item for item in runtime)
    assert any(item.startswith("httpx") and "<0.29" in item for item in dev)

    constraints = {
        line.strip()
        for line in (REPO_ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert {
        "fastapi<0.136",
        "starlette<1.0",
        "anyio<4.13",
        "httpx<0.29",
    }.issubset(constraints)

    ci_workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert 'pip install -c constraints.txt -e ".[dev]"' in ci_workflow


def test_documented_workflow_uses_post_generation_review_routing() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    architecture = (REPO_ROOT / "docs" / "architecture.md").read_text(
        encoding="utf-8"
    )

    assert "FR[final_review_router]" in readme
    assert "FR -->|review required| HR[human_review_handoff]" in readme
    assert "JA -->|review required| HR[human_review_handoff]" not in readme

    assert "JA[judge_agent] --> AG[answer_generator]" in architecture
    assert 'AG --> FR["final_review_router"]' in architecture
    assert 'FR --> POLICY["should_handoff_for_review"]' in architecture
    assert 'POLICY -->|no| AG[answer_generator]' not in architecture


def test_ci_eval_documentation_includes_retrieval_gate() -> None:
    docs = (REPO_ROOT / "docs" / "ci_eval_gate.md").read_text(encoding="utf-8")

    assert "Run retrieval eval gate" in docs
    assert "--min-score 0.90" in docs
    assert "data/retrieval_eval_results.json" in docs
    assert "data/retrieval_eval_report.md" in docs
