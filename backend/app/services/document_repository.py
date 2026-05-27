"""DocumentRepository — the single seam between retrievers and the
ingested document store.

The repository hides three loading strategies behind one interface so
the LangGraph nodes never need to know where the data came from:

1. **JSON store** (preferred) — read ``data/trustrag_documents.json``
   produced by ``backend.app.ingestion.ingest_sample_docs``.
2. **Live sample_docs/** — load the Markdown files directly.
3. **Hardcoded mock fallback** — preserves the Phase 1 records so the
   process can boot even with no ingestion done and no sample_docs
   reachable.

Phase 3 will swap the in-memory keyword scan for a real hybrid
retriever (Qdrant + BM25 + reranker) without touching any node.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Lock

from ..ingestion.markdown_loader import load_markdown_documents
from ..ingestion.models import AccountingDocument

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_STORE = _PROJECT_ROOT / "data" / "trustrag_documents.json"
_DEFAULT_SAMPLE_DIR = _PROJECT_ROOT / "sample_docs"


# ---------------------------------------------------------------------------
# Client + keyword mappings
# ---------------------------------------------------------------------------


# (alias_substring_lowered, canonical_client_name)
_CLIENT_ALIASES: tuple[tuple[str, str], ...] = (
    ("alpha trading", "Alpha Trading Co."),
    ("alpha", "Alpha Trading Co."),
    ("beta catering", "Beta Catering Ltd."),
    ("beta", "Beta Catering Ltd."),
    ("gamma tech", "Gamma Tech Studio"),
    ("gamma", "Gamma Tech Studio"),
)


# (substring, target document_type)
_TYPE_HINTS: tuple[tuple[str, str], ...] = (
    ("入账", "bookkeeping_sop"),
    ("做账", "bookkeeping_sop"),
    ("记账", "bookkeeping_sop"),
    ("科目", "bookkeeping_sop"),
    ("bookkeeping", "bookkeeping_sop"),
    ("ledger", "bookkeeping_sop"),
    ("发票", "invoice_compliance"),
    ("invoice", "invoice_compliance"),
    ("备注", "invoice_compliance"),
    ("服务描述", "invoice_compliance"),
    ("专票", "invoice_compliance"),
    ("普票", "invoice_compliance"),
    ("报销", "reimbursement_policy"),
    ("reimbursement", "reimbursement_policy"),
    ("差旅", "reimbursement_policy"),
    ("打车", "reimbursement_policy"),
    ("住宿", "reimbursement_policy"),
    ("hotel", "reimbursement_policy"),
    ("taxi", "reimbursement_policy"),
    ("增值税", "tax_policy_note"),
    ("vat", "tax_policy_note"),
    ("小规模纳税人", "tax_policy_note"),
    ("纳税人", "tax_policy_note"),
    ("tax", "tax_policy_note"),
    ("税", "tax_policy_note"),
    ("资料", "document_checklist"),
    ("清单", "document_checklist"),
    ("checklist", "document_checklist"),
    ("bank statement", "document_checklist"),
)


# Keyword triggers for the adversarial sample. The malicious doc only
# surfaces when the question explicitly references following document
# instructions; otherwise it stays out of retrieval entirely.
_MALICIOUS_TRIGGERS = (
    "ignore",
    "previous instructions",
    "指令",
    "instructions",
    "照做",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _question_client(question: str) -> str | None:
    q = (question or "").lower()
    for alias, canonical in _CLIENT_ALIASES:
        if alias in q:
            return canonical
    return None


def _question_types(question: str) -> set[str]:
    """Return the set of document_types the question seems to ask about."""

    q = (question or "").lower()
    hits: set[str] = set()
    for sub, doc_type in _TYPE_HINTS:
        if sub in q or sub.lower() in q:
            hits.add(doc_type)
    return hits


def _is_malicious_query(question: str) -> bool:
    q = (question or "").lower()
    return any(t.lower() in q for t in _MALICIOUS_TRIGGERS)


# ---------------------------------------------------------------------------
# Mock fallback — only used when neither JSON store nor sample_docs are
# reachable. Kept as plain dicts so we don't reintroduce the old import
# path.
# ---------------------------------------------------------------------------


def _hardcoded_fallback() -> list[AccountingDocument]:
    """Last-resort fallback so the workflow still boots on a bare checkout."""

    seed = (
        dict(
            document_id="reimbursement_policy_2024",
            title="Client Reimbursement Policy (2024)",
            version="2024_v1",
            valid_from="2024-01-01",
            valid_to="2025-12-31",
            document_type="reimbursement_policy",
            policy_family="reimbursement_policy",
            content=(
                "Taxi expenses under 200 RMB do not require approval. "
                "Hotel expenses under 300 RMB per night can be reimbursed."
            ),
            source_path="<hardcoded-fallback>",
            checksum="fallback",
        ),
        dict(
            document_id="reimbursement_policy_2026",
            title="Client Reimbursement Policy (2026)",
            version="2026_v1",
            valid_from="2026-01-01",
            valid_to=None,
            document_type="reimbursement_policy",
            policy_family="reimbursement_policy",
            replaces="reimbursement_policy_2024",
            content=(
                "Taxi expenses over 100 RMB require manager approval. "
                "Hotel expenses over 200 RMB per night require travel "
                "approval form."
            ),
            source_path="<hardcoded-fallback>",
            checksum="fallback",
        ),
    )
    return [AccountingDocument(**rec) for rec in seed]


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class DocumentRepository:
    """Lazy-loaded read-only document repository with client-aware search."""

    def __init__(
        self,
        store_path: Path | None = None,
        sample_dir: Path | None = None,
    ) -> None:
        self.store_path = Path(store_path) if store_path else _DEFAULT_STORE
        self.sample_dir = Path(sample_dir) if sample_dir else _DEFAULT_SAMPLE_DIR
        self._documents: list[AccountingDocument] | None = None
        self._lock = Lock()
        self._source: str | None = None

    # -- Loading ---------------------------------------------------------

    def load_documents(self) -> list[AccountingDocument]:
        """Return the canonical list of ingested documents (cached)."""

        with self._lock:
            if self._documents is not None:
                return list(self._documents)

            documents: list[AccountingDocument]
            if self.store_path.exists():
                documents = self._load_from_json(self.store_path)
                self._source = f"json:{self.store_path}"
            elif self.sample_dir.exists() and any(self.sample_dir.glob("*.md")):
                documents = load_markdown_documents(self.sample_dir)
                self._source = f"sample_docs:{self.sample_dir}"
            else:
                documents = _hardcoded_fallback()
                self._source = "hardcoded-fallback"
                logger.warning(
                    "DocumentRepository falling back to hardcoded seed; "
                    "no JSON store at %s and no markdown in %s",
                    self.store_path,
                    self.sample_dir,
                )

            self._documents = documents
            return list(self._documents)

    @staticmethod
    def _load_from_json(path: Path) -> list[AccountingDocument]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_docs = payload.get("documents", [])
        return [AccountingDocument(**doc) for doc in raw_docs]

    @property
    def source(self) -> str | None:
        """Where the documents came from (set after first ``load_documents``)."""

        if self._documents is None:
            self.load_documents()
        return self._source

    # -- Retrieval -------------------------------------------------------

    def search(
        self,
        question: str,
        *,
        stance: str = "support",
        client: str | None = None,
        limit: int = 5,
    ) -> list[dict]:
        """Return ranked evidence dicts for the question.

        ``stance="support"`` returns evidence supporting an answer (the
        currently-effective version). ``stance="counter"`` returns
        evidence that may contradict or supersede the support set
        (historical versions, restrictive caveats).
        """

        documents = self.load_documents()
        target_types = _question_types(question)
        question_client = client or _question_client(question)
        wants_malicious = _is_malicious_query(question)

        hits: list[dict] = []
        for doc in documents:
            score = self._score_document(
                doc,
                target_types=target_types,
                question_client=question_client,
                wants_malicious=wants_malicious,
                stance=stance,
            )
            if score <= 0.0:
                continue
            hits.append(doc.to_evidence_dict(stance=stance, score=score))

        hits.sort(key=lambda h: h["score"], reverse=True)
        return hits[:limit]

    @staticmethod
    def _score_document(
        doc: AccountingDocument,
        *,
        target_types: set[str],
        question_client: str | None,
        wants_malicious: bool,
        stance: str,
    ) -> float:
        # Adversarial samples only enter retrieval when the question
        # explicitly mentions following document instructions. They are
        # scored low and always returned as counter so the safety_checker
        # can act on them without them being treated as primary support.
        if doc.is_malicious:
            if not wants_malicious:
                return 0.0
            return 0.15 if stance == "counter" else 0.0

        # Relevance gate — we need at least ONE strong signal before
        # returning a non-malicious record. Without this, a question
        # like "should I follow document instructions?" would surface
        # every accounting policy in the corpus.
        has_type_match = bool(target_types) and doc.document_type in target_types
        has_client_match = (
            question_client is not None and doc.client == question_client
        )
        if not has_type_match and not has_client_match:
            return 0.0

        # Client filter — when the question names a specific client, we
        # only return that client's docs and firm-wide docs (client is None).
        if question_client is not None:
            if doc.client is not None and doc.client != question_client:
                return 0.0

        # Stance routing.
        is_historical = bool(doc.valid_to)
        if stance == "support" and is_historical:
            return 0.0
        if stance == "counter" and not is_historical:
            return 0.0

        # Score baseline.
        score = 0.9 if stance == "support" else 0.7
        if question_client and doc.client == question_client:
            score += 0.05
        return round(score, 3)

    # -- Diagnostics -----------------------------------------------------

    def describe(self) -> list[dict]:
        """Small projection used by the GET /v1/documents endpoint."""

        return [
            {
                "document_id": d.document_id,
                "title": d.title,
                "version": d.version,
                "document_type": d.document_type,
                "client": d.client,
                "valid_from": d.valid_from,
                "valid_to": d.valid_to,
                "policy_family": d.policy_family,
                "replaces": d.replaces,
                "is_malicious": d.is_malicious,
                "source_path": d.source_path,
            }
            for d in self.load_documents()
        ]


# ---------------------------------------------------------------------------
# Module-level singleton — most callers should use this rather than
# constructing their own repository, so the JSON load happens once per
# process.
# ---------------------------------------------------------------------------


_repository_singleton: DocumentRepository | None = None
_singleton_lock = Lock()


def get_repository() -> DocumentRepository:
    global _repository_singleton
    with _singleton_lock:
        if _repository_singleton is None:
            _repository_singleton = DocumentRepository()
        return _repository_singleton


def reset_repository() -> None:
    """Reset the singleton — used by tests that want a clean load."""

    global _repository_singleton
    with _singleton_lock:
        _repository_singleton = None
