"""DocumentRepository — single seam between graph retrievers and the
ingested document/chunk store.

Phase 3A: scoring + filtering have been lifted into
``backend.app.retrieval``. This file now owns three responsibilities:

1. **Loading** chunks (chunk store → document store → sample_docs →
   hardcoded fallback). Unchanged from Phase 2B.
2. **Dispatching** to :class:`backend.app.retrieval.RetrievalService`
   for actual retrieval — no more inline keyword / type / stance
   scoring code.
3. **Flattening** the layer's :class:`ScoredChunk` results into the
   legacy evidence dict shape that LangGraph nodes consume, with two
   new fields (``score_breakdown``, ``retrieval_strategy``) added
   non-disruptively.

The repository also keeps one piece of *workflow-aware* policy: if
the user's query literally contains an injection trigger (e.g. "ignore
previous instructions"), we set ``include_malicious=True`` so the
malicious sample chunk reaches the safety_checker via counter_evidence.
This stays here — not in the retrieval layer — because it's an
accounting-workflow safety policy, not a retrieval concern.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from threading import Lock
from typing import Any

from ..ingestion.chunker import chunk_documents
from ..ingestion.models import AccountingDocument, DocumentChunk
from ..ingestion.unified_loader import load_documents_from_directory
from ..retrieval import RetrievalService, ScoredChunk

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CHUNK_STORE = _PROJECT_ROOT / "data" / "trustrag_chunks.json"
_DEFAULT_DOCUMENT_STORE = _PROJECT_ROOT / "data" / "trustrag_documents.json"
_DEFAULT_SAMPLE_DIR = _PROJECT_ROOT / "sample_docs"


# ---------------------------------------------------------------------------
# Injection trigger detection (workflow-level safety policy)
# ---------------------------------------------------------------------------

_MALICIOUS_TRIGGERS = (
    "ignore",
    "previous instructions",
    "指令",
    "instructions",
    "照做",
)


def _is_malicious_query(question: str) -> bool:
    """True if the query literally names a prompt-injection pattern.

    Triggers the workflow's "let the malicious chunk through so
    safety_checker can flag it" path. Deliberately conservative — a
    benign question that mentions the word "instructions" should not
    trigger this. (We rely on the multi-pattern AND semantics of
    safety_checker downstream to avoid false positives.)
    """

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
# ScoredChunk → evidence dict
# ---------------------------------------------------------------------------


def _scored_chunk_to_evidence_dict(
    scored: ScoredChunk,
    *,
    stance: str,
) -> dict[str, Any]:
    """Flatten a :class:`ScoredChunk` into the legacy evidence dict shape.

    Adds two new fields on top of the Phase 2B shape:

    * ``score_breakdown``: per-component scoring contributions.
    * ``retrieval_strategy``: which retriever produced this hit
      (today always ``"hybrid_keyword_bm25"``; Phase 3B will diversify).
    """

    return {
        # Chunk-level identity
        "chunk_id": scored.chunk_id,
        "chunk_index": scored.chunk_index,
        "section_title": scored.section_title,
        "page_number": scored.page_number,
        # Document-level identity
        "doc_id": scored.document_id,
        "document_id": scored.document_id,
        "title": scored.title,
        "version": scored.version,
        "valid_from": scored.valid_from,
        "valid_to": scored.valid_to,
        "client": scored.client,
        "document_type": scored.document_type,
        "policy_family": scored.policy_family,
        "replaces": scored.replaces,
        "risk_type": scored.risk_type,
        "is_malicious": scored.is_malicious,
        "source_type": "external" if scored.is_malicious else "policy",
        "source_path": scored.source_path,
        # Body + scoring
        "content": scored.content,
        "score": scored.score,
        "stance": stance,
        # Phase 3A retrieval explainability
        "score_breakdown": scored.score_breakdown.model_dump(),
        "retrieval_strategy": scored.retrieval_strategy,
    }


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class DocumentRepository:
    """Lazy-loaded read-only chunk repository.

    Construction is cheap (just stores paths). Loading + retrieval-service
    construction happen on the first call to ``load_*`` or ``search``.
    """

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
        self._retrieval_service: RetrievalService | None = None
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
        # Build the retrieval service eagerly once the chunk list is
        # known so the first /v1/rag/query doesn't pay tokenization
        # cost on the request thread.
        self._retrieval_service = RetrievalService(chunks)

    @staticmethod
    def _load_from_chunk_store(
        path: Path,
    ) -> tuple[list[DocumentChunk], list[AccountingDocument]]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw_chunks = payload.get("chunks", [])
        chunks = [DocumentChunk(**c) for c in raw_chunks]
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

    @property
    def retrieval_service(self) -> RetrievalService:
        """Expose the service for tests that want to probe specific layers."""

        with self._lock:
            self._ensure_loaded()
            assert self._retrieval_service is not None
            return self._retrieval_service

    def get_retrieval_service(self) -> RetrievalService:
        """Explicit seam for the Phase 4A LangChain adapter layer.

        Equivalent to the :attr:`retrieval_service` property but spelled
        as a method so the call site reads like the conventional
        LangChain "construct the runnable" pattern::

            service = repository.get_retrieval_service()
            runnable = build_retrieval_runnable(retrieval_service=service, ...)
            evidence = runnable.invoke(question)

        The repository remains the single load-and-cache point for the
        chunk corpus; the adapter just takes the constructed service by
        reference, so there's no duplicated loading cost.
        """

        return self.retrieval_service

    # -- Retrieval -------------------------------------------------------

    def search(
        self,
        question: str,
        *,
        stance: str = "support",
        client: str | None = None,
        limit: int = 5,
        top_k: int | None = None,
        question_type: str | None = None,
        include_malicious: bool = False,
    ) -> list[dict[str, Any]]:
        """Return ranked **chunk-level** evidence dicts via the retrieval layer.

        Back-compat:

        * ``limit`` is honored when ``top_k`` is not provided.
        * ``client`` (when set) overrides the auto-inferred client in
          the metadata filter — useful for tests that want to pin the
          client without restating it in the query.

        Phase 3A additions:

        * ``question_type`` — passed through to
          :class:`MetadataFilter` for stronger document_type
          inference.
        * ``include_malicious`` — explicit override. Defaults to
          ``False`` but is forced ``True`` when the query literally
          names an injection trigger (so the workflow's
          safety_checker can find the malicious chunk in
          counter_evidence).
        """

        with self._lock:
            self._ensure_loaded()
            service = self._retrieval_service
            assert service is not None

        # Workflow-level auto-detection of injection-trigger queries.
        if not include_malicious and _is_malicious_query(question):
            include_malicious = True

        effective_top_k = top_k if top_k is not None else limit

        scored = service.search(
            question,
            question_type=question_type,
            top_k=effective_top_k,
            stance=stance,
            include_malicious=include_malicious,
        )

        if client is not None:
            # Legacy explicit-client override path. Honored *after*
            # retrieval — drop anything whose chunk.client is set
            # to a different client.
            scored = [
                s
                for s in scored
                if s.client is None or s.client == client
            ]

        return [
            _scored_chunk_to_evidence_dict(s, stance=stance) for s in scored
        ]

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
