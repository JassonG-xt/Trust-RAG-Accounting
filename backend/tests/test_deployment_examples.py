from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_deployment_examples_script_passes_in_repo_root() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to run the deployment examples script")

    script = REPO_ROOT / "scripts" / "check_deployment_examples.sh"
    assert script.is_file()

    result = subprocess.run(
        [bash, "scripts/check_deployment_examples.sh"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "[deployment-examples] OK" in result.stdout


def test_render_blueprint_declares_public_demo_web_service() -> None:
    path = REPO_ROOT / "render.yaml"
    assert path.is_file()

    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    services = config.get("services") or []
    service = next(
        (item for item in services if item.get("name") == "trust-rag-accounting-demo"),
        None,
    )

    assert service is not None
    assert service["type"] == "web"
    assert service["runtime"] == "python"
    assert service["plan"] == "free"
    assert service["healthCheckPath"] == "/healthz"
    assert service["startCommand"] == "bash scripts/run_render_demo.sh"
    assert service["buildCommand"] == "pip install -c constraints.txt -e ."
    env = {item["key"]: item.get("value") for item in service.get("envVars", [])}
    assert env["LLM_ANSWER_MODE"] == "template"
    assert env["EMBEDDING_PROVIDER"] == "mock"
    assert env["VECTOR_STORE"] == "memory"
    assert env["RERANKER_PROVIDER"] == "mock"
    assert env["TRUSTRAG_PUBLIC_DEMO_ENABLED"] == "true"
    assert env["TRUSTRAG_TRACE_ENABLED"] == "false"


def test_render_demo_start_script_ingests_sample_docs_and_starts_uvicorn() -> None:
    path = REPO_ROOT / "scripts" / "run_render_demo.sh"
    assert path.is_file()

    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    if os.name != "nt":
        assert path.stat().st_mode & 0o111
    assert "resolve_python" in text
    assert '[[ -x ".venv/bin/python" ]]' in text
    assert 'PYTHON_BIN="${PYTHON_BIN:-$(resolve_python)}"' in text
    assert '"$PYTHON_BIN" -m backend.app.ingestion.ingest_sample_docs' in text
    assert "--source sample_docs" in text
    assert "--documents-out data/trustrag_documents.json" in text
    assert "--chunks-out data/trustrag_chunks.json" in text
    assert '"$PYTHON_BIN" -m uvicorn backend.app.main:app' in text
    assert "--host 0.0.0.0" in text
    assert '--port "${PORT:-8000}"' in text


def test_readme_has_live_rag_demo_button_to_hosted_dashboard() -> None:
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "[![Live RAG Demo]" in text
    assert "https://trust-rag-accounting-demo.onrender.com/dashboard" in text
    assert "FastAPI-local%20demo" not in text
    assert "free Render instance may cold start" in text
    assert "fictional sample docs" in text
    assert "not accounting or tax advice" in text
    assert "| Phase | 10D" in text
    assert "passing locally" not in text
    assert "- Phase 9C:" not in text


def test_production_deployment_docs_use_runtime_constraints_not_dev_extra() -> None:
    paths = [
        REPO_ROOT / "render.yaml",
        REPO_ROOT / "docs" / "small_server_deployment.md",
        REPO_ROOT / "docs" / "deploy_examples" / "systemd" / "trustrag-accounting.service.example",
    ]

    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert ".[dev]" not in text, path

    small_server = paths[1].read_text(encoding="utf-8")
    assert "pip install -c constraints.txt -e ." in small_server
    assert "/opt/trustrag-accounting/app/\n|-- .venv/\n|-- data/" in small_server
    assert (
        "sudo -u trustrag env PYTHON=.venv/bin/python bash scripts/run_eval_gate.sh"
        in small_server
    )
