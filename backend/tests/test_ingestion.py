"""Tests for the TrustRAG Phase 2A ingestion layer.

Three groups of tests:

1. Front-matter parser unit tests.
2. End-to-end ingestion CLI on the real ``sample_docs/`` corpus.
3. ``DocumentRepository`` search behavior against the ingested store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.ingestion.frontmatter import (
    FrontMatterError,
    MissingFrontMatterError,
    parse_frontmatter_markdown,
)
from backend.app.ingestion.ingest_sample_docs import ingest
from backend.app.ingestion.markdown_loader import (
    load_markdown_document,
    load_markdown_documents,
)
from backend.app.services.document_repository import (
    DocumentRepository,
    reset_repository,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SAMPLE_DOCS = PROJECT_ROOT / "sample_docs"


# ---------------------------------------------------------------------------
# Group 1 — front-matter parser
# ---------------------------------------------------------------------------


def test_frontmatter_parses_metadata_and_body():
    raw = (
        "---\n"
        "title: Test Doc\n"
        "version: 1\n"
        "valid_from: 2026-01-01\n"
        "valid_to:\n"
        "---\n"
        "\n"
        "# Body\n"
        "\n"
        "Body content here.\n"
    )
    metadata, body = parse_frontmatter_markdown(raw)
    assert metadata["title"] == "Test Doc"
    assert metadata["version"] == 1
    assert metadata["valid_from"] == "2026-01-01"
    # Empty YAML value must come out as None.
    assert metadata["valid_to"] is None
    assert body.startswith("# Body")
    assert body.endswith("Body content here.")


def test_frontmatter_missing_raises():
    with pytest.raises(MissingFrontMatterError):
        parse_frontmatter_markdown("# Just a heading, no YAML\n")


def test_frontmatter_unclosed_raises():
    raw = "---\ntitle: oops\n# no closing delimiter\n"
    with pytest.raises(FrontMatterError):
        parse_frontmatter_markdown(raw)


def test_frontmatter_invalid_yaml_raises():
    raw = "---\ntitle: ok\nbroken: [unclosed\n---\nbody\n"
    with pytest.raises(FrontMatterError):
        parse_frontmatter_markdown(raw)


def test_markdown_loader_requires_required_fields(tmp_path: Path):
    bad = tmp_path / "missing_title.md"
    bad.write_text(
        "---\nversion: 1\ndocument_type: bookkeeping_sop\n---\nbody\n",
        encoding="utf-8",
    )
    with pytest.raises(FrontMatterError):
        load_markdown_document(bad)


# ---------------------------------------------------------------------------
# Group 2 — ingestion CLI against the real sample_docs/
# ---------------------------------------------------------------------------


def test_ingest_sample_docs_produces_json_store(tmp_path: Path):
    out_path = tmp_path / "trustrag_documents.json"
    summary = ingest(SAMPLE_DOCS, out_path, quiet=True)

    assert out_path.exists()
    assert summary["document_count"] >= 7

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["count"] == summary["document_count"]
    doc_ids = {d["document_id"] for d in payload["documents"]}
    assert "reimbursement_policy_2024" in doc_ids
    assert "reimbursement_policy_2026" in doc_ids
    assert "alpha_trading_bookkeeping_sop_2026" in doc_ids
    assert "beta_catering_invoice_rule_2026" in doc_ids
    assert "vat_policy_note_2025" in doc_ids
    assert "monthly_bookkeeping_checklist_2026" in doc_ids
    assert "malicious_accounting_instruction_sample" in doc_ids

    by_id = {d["document_id"]: d for d in payload["documents"]}
    assert by_id["alpha_trading_bookkeeping_sop_2026"]["client"] == "Alpha Trading Co."
    assert by_id["beta_catering_invoice_rule_2026"]["client"] == "Beta Catering Ltd."
    assert by_id["malicious_accounting_instruction_sample"]["is_malicious"] is True
    assert by_id["reimbursement_policy_2026"]["replaces"] == "reimbursement_policy_2024"


def test_load_markdown_documents_is_stable_order():
    docs_a = load_markdown_documents(SAMPLE_DOCS)
    docs_b = load_markdown_documents(SAMPLE_DOCS)
    assert [d.document_id for d in docs_a] == [d.document_id for d in docs_b]


# ---------------------------------------------------------------------------
# Group 3 — DocumentRepository search behavior
# ---------------------------------------------------------------------------


@pytest.fixture
def repository(tmp_path: Path) -> DocumentRepository:
    out_path = tmp_path / "trustrag_documents.json"
    ingest(SAMPLE_DOCS, out_path, quiet=True)
    return DocumentRepository(store_path=out_path)


def test_repository_loads_from_ingested_json(repository: DocumentRepository):
    docs = repository.load_documents()
    assert len(docs) >= 7
    assert repository.source and repository.source.startswith("json:")


def test_repository_client_filter_alpha_excludes_beta(repository: DocumentRepository):
    hits = repository.search(
        "Alpha Trading Co. 的餐饮发票应该怎么入账？",
        stance="support",
    )
    doc_ids = {h["doc_id"] for h in hits}
    assert "alpha_trading_bookkeeping_sop_2026" in doc_ids
    assert "beta_catering_invoice_rule_2026" not in doc_ids


def test_repository_client_filter_beta_excludes_alpha(repository: DocumentRepository):
    hits = repository.search(
        "Beta Catering Ltd. 没有明确服务描述的配送发票能直接入账吗？",
        stance="support",
    )
    doc_ids = {h["doc_id"] for h in hits}
    assert "beta_catering_invoice_rule_2026" in doc_ids
    assert "alpha_trading_bookkeeping_sop_2026" not in doc_ids


def test_repository_reimbursement_returns_both_versions(repository: DocumentRepository):
    support = repository.search("现在打车超过 100 元需要审批吗？", stance="support")
    counter = repository.search("现在打车超过 100 元需要审批吗？", stance="counter")
    support_ids = {h["doc_id"] for h in support}
    counter_ids = {h["doc_id"] for h in counter}
    assert "reimbursement_policy_2026" in support_ids
    assert "reimbursement_policy_2024" in counter_ids


def test_repository_vat_returns_vat_note(repository: DocumentRepository):
    hits = repository.search(
        "小规模纳税人现在增值税应该怎么处理？",
        stance="support",
    )
    doc_ids = {h["doc_id"] for h in hits}
    assert "vat_policy_note_2025" in doc_ids


def test_repository_malicious_only_on_injection_trigger(
    repository: DocumentRepository,
):
    clean = repository.search("现在打车超过 100 元需要审批吗？", stance="counter")
    assert all(not h.get("is_malicious") for h in clean)

    triggered = repository.search(
        "文档里说 Ignore previous instructions，系统应该照做吗？",
        stance="counter",
    )
    assert any(h.get("is_malicious") for h in triggered)


# ---------------------------------------------------------------------------
# Cleanup — reset the global repository so subsequent test modules pick
# up the real on-disk store rather than a stale cache.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_global_repository():
    reset_repository()
    yield
    reset_repository()
