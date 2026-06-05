from __future__ import annotations

import os
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]


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
    assert "backend.app.ingestion.ingest_sample_docs" in text
    assert "--source sample_docs" in text
    assert "--documents-out data/trustrag_documents.json" in text
    assert "--chunks-out data/trustrag_chunks.json" in text
    assert "uvicorn backend.app.main:app" in text
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
