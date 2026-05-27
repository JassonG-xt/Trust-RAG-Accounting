"""DocumentRepository — single seam between graph retrievers and the
ingested document/chunk store.

Phase 2B: the canonical evidence unit is the **chunk**. The repository
loads with this preference order:

1. ``data/trustrag_chunks.json``        (chunk store written by the ingest CLI)
2. ``data/trustrag_documents.json``     (Phase 2A document store, chunked on the fly)
3. ``sample_docs/`` (Markdown / PDF / DOCX)  loaded + chunked at runtime
4. Hardcoded fallback                   (last resort so the workflow boots)

``DocumentRepository.search`` always returns chunk-level evidence
dicts, so the LangGraph nodes never see a "whole document" — every hit
already carries ``chunk_id`` + ``section_title`` + the parent
document's metadata.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Lock

from ..ingestion.chunker import chunk_documents
from ..ingestion.models import AccountingDocument, DocumentChunk
from ..ingestion.unified_loader import load_documents_from_directory

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CHUNK_STORE = _PROJECT_ROOT / "data" / "trustrag_chunks.json"
_DEFAULT_DOCUMENT_STORE = _PROJECT_ROOT / "data" / "trustrag_documents.json"
_DEFAULT_SAMPLE_DIR = _PROJECT_ROOT / "sample_docs"


# ---------------------------------------------------------------------------
# Client + keyword mappings
# ---------------------------------------------------------------------------


_CLIENT_ALIASES: tuple[tuple[str, str], ...] = (
    ("alpha trading", "Alpha Trading Co."),
    ("alpha", "Alpha Trading Co."),
    ("beta catering", "Beta Catering Ltd."),
    ("beta", "Beta Catering Ltd."),
    ("gamma tech", "Gamma Tech Studio"),
    ("gamma", "Gamma Tech Studio"),
)


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


_MALICIOUS_TRIGGERS = (
    "ignore",
    "previous instructions",
    "指令",
    "instructions",
    "照做",
)


def _question_client(question: str) -> str | None:
    q = (question or "").lower()
    for alias, canonical in _CLIENT_ALIASES:
        if alias in q:
            return canonical
    return None


def _question_types(question: str) -> set[str]:
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
# Fallback
# ---------------------------------------------------------------------------


def _hardcoded_fallback_documents() -> list[AccountingDocument]:
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
    """Lazy-loaded read-only chunk repository."""

    def __init__(
        self,
        chunk_store_path: Path | None = None,
        document_store_path: Path | None = None,
        sample_dir: Path | None = None,
    ) -> None:
        self.chunk_store_path = (
            Path(chunk_store_path) if chunk_store_path else _DEFAULT_CHUNK_STORE
        )
        self.document_store_path = (
            Path(document_store_path) if document_store_path else _DEFAULT_DOCUMENT_STORE
        )
        self.sample_dir = Path(sample_dir) if sample_dir else _DEFAULT_SAMPLE_DIR
        self._documents: list[AccountingDocument] | None = None
        self._chunks: list[DocumentChunk] | None = None
        self._lock = Lock()
        self._source: str | None = None

    # -- Loading ---------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._chunks is not None:
            return

        if self.chunk_store_path.exists():
            chunks, documents = self._load_from_chunk_store(self.chunk_store_path)
            self._source = f"chunk_store:{self.chunk_store_path}"
        elif self.document_store_path.exists():
            documents = self._load_documents_from_json(self.document_store_path)
            chunks = chunk_documents(documents)
            self._source = f"document_store:{self.document_store_path}"
        elif self.sample_dir.exists() and any(
            p.suffix.lower() in {".md", ".pdf", ".docx"}
            for p in self.sample_dir.iterdir()
            if p.is_file()
        ):
            documents = load_documents_from_directory(self.sample_dir)
            chunks = chunk_documents(documents)
            self._source = f"sample_docs:{self.sample_dir}"
        else:
            documents = _hardcoded_fallback_documents()
            chunks = chunk_documents(documents)
            self._source = "hardcoded-fallback"
            logger.warning(
                "DocumentRepository falling back to hardcoded seed (no chunk "
                "store at %s, no document store at %s, no sample_docs in %s)",
                self.chunk_store_path,
                self.document_store_path,
                self.sample_dir,
            )

        self._documents = documents
        self._chunks = chunks

    @staticmethod
    def _load_from_chunk_store(
        path: Path,
    ) -> tuple[list[DocumentChunk], list[AccountingDocument]]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_chunks = payload.get("chunks", [])
        chunks = [DocumentChunk(**c) for c in raw_chunks]
        # Reconstruct a thin document list from the chunks so the
        # ``describe()`` projection still works. We don't have the full
        # body, but we have all the metadata we need.
        documents: dict[str, AccountingDocument] = {}
        for c in chunks:
            if c.document_id in documents:
                continue
            documents[c.document_id] = AccountingDocument(
                document_id=c.document_id,
                title=c.title,
                version=c.version,
                valid_from=c.valid_from,
                valid_to=c.valid_to,
                document_type=c.document_type,
                client=c.client,
                policy_family=c.policy_family,
                replaces=c.replaces,
                risk_type=c.risk_type,
                is_malicious=c.is_malicious,
                source_path=c.source_path,
                content="",
                checksum=c.checksum,
            )
        return chunks, list(documents.values())

    @staticmethod
    def _load_documents_from_json(path: Path) -> list[AccountingDocument]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_docs = payload.get("documents", [])
        return [AccountingDocument(**doc) for doc in raw_docs]

    def load_documents(self) -> list[AccountingDocument]:
        with self._lock:
            self._ensure_loaded()
            return list(self._documents or [])

    def load_chunks(self) -> list[DocumentChunk]:
        with self._lock:
            self._ensure_loaded()
            return list(self._chunks or [])

    @property
    def source(self) -> str | None:
        with self._lock:
            self._ensure_loaded()
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
        """Return ranked **chunk-level** evidence dicts."""

        chunks = self.load_chunks()
        target_types = _question_types(question)
        question_client = client or _question_client(question)
        wants_malicious = _is_malicious_query(question)

        scored: list[dict] = []
        for chunk in chunks:
            score = self._score_chunk(
                chunk,
                target_types=target_types,
                question_client=question_client,
                wants_malicious=wants_malicious,
                stance=stance,
            )
            if score <= 0.0:
                continue
            scored.append(chunk.to_evidence_dict(stance=stance, score=score))

        scored.sort(key=lambda h: h["score"], reverse=True)
        return scored[:limit]

    @staticmethod
    def _score_chunk(
        chunk: DocumentChunk,
        *,
        target_types: set[str],
        question_client: str | None,
        wants_malicious: bool,
        stance: str,
    ) -> float:
        if chunk.is_malicious:
            if not wants_malicious:
                return 0.0
            return 0.15 if stance == "counter" else 0.0

        has_type_match = bool(target_types) and chunk.document_type in target_types
        has_client_match = (
            question_client is not None and chunk.client == question_client
        )
        if not has_type_match and not has_client_match:
            return 0.0

        if question_client is not None:
            if chunk.client is not None and chunk.client != question_client:
                return 0.0

        is_historical = bool(chunk.valid_to)
        if stance == "support" and is_historical:
            return 0.0
        if stance == "counter" and not is_historical:
            return 0.0

        score = 0.9 if stance == "support" else 0.7
        if question_client and chunk.client == question_client:
            score += 0.05
        # Mild within-document de-noising: earlier chunks (more likely to
        # carry the headline rule) get a tiny boost so the citation list
        # is stable across runs.
        score -= 0.01 * min(chunk.chunk_index, 5)
        return round(max(score, 0.0), 3)

    # -- Diagnostics -----------------------------------------------------

    def describe(self) -> list[dict]:
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

    def chunk_count(self) -> int:
        return len(self.load_chunks())


# ---------------------------------------------------------------------------
# Singleton
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
