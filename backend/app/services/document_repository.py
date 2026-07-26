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

import contextvars
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
# Phase 10C — derived wiki chunk store (mirrors wiki.apply's default output at
# ``<wiki_dir>.parent/trustrag_wiki_chunks.json``). Gitignored under data/; it
# only exists once a wiki proposal has been approved + applied.
_DEFAULT_WIKI_CHUNK_STORE = _PROJECT_ROOT / "data" / "trustrag_wiki_chunks.json"
# A path guaranteed not to exist — used to disable the document-store / sample-dir
# fallback legs for the wiki corpus (it loads only from the wiki chunk store).
_NO_STORE = _PROJECT_ROOT / "data" / "__no_such_store__"


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
        "is_context_expansion": scored.is_context_expansion,
        "expanded_from_chunk_id": scored.expanded_from_chunk_id,
        "expansion_offset": scored.expansion_offset,
        # Phase 3A retrieval explainability
        "score_breakdown": scored.score_breakdown.model_dump(),
        "retrieval_strategy": scored.retrieval_strategy,
        "fusion_method": scored.metadata.get("fusion_method"),
        "source_ranks": scored.metadata.get("source_ranks", {}),
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
        *,
        allow_fallback: bool = True,
    ) -> None:
        self.chunk_store_path = (
            Path(chunk_store_path) if chunk_store_path else _DEFAULT_CHUNK_STORE
        )
        self.document_store_path = (
            Path(document_store_path) if document_store_path else _DEFAULT_DOCUMENT_STORE
        )
        self.sample_dir = Path(sample_dir) if sample_dir else _DEFAULT_SAMPLE_DIR
        # When False (the wiki / hybrid corpora), a missing store yields an
        # *empty* corpus rather than the hardcoded raw seed — serving raw seed
        # docs under RETRIEVAL_SOURCE=wiki would be a silent trust violation.
        self.allow_fallback = allow_fallback
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
            documents = _hardcoded_fallback_documents() if self.allow_fallback else []
            chunks = chunk_documents(documents) if documents else []
            self._source = "hardcoded-fallback" if self.allow_fallback else "empty"
            if self.allow_fallback:
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
        return chunks, DocumentRepository._documents_from_chunks(chunks)

    @staticmethod
    def _documents_from_chunks(
        chunks: list[DocumentChunk],
    ) -> list[AccountingDocument]:
        """Derive one document-level record per document_id from chunks."""

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
        return list(documents.values())

    @classmethod
    def from_chunks(
        cls,
        chunks: list[DocumentChunk],
        *,
        source: str = "in-memory",
        wiki_page_ids: set[str] | None = None,
    ) -> DocumentRepository:
        """Build a preloaded repository over an explicit chunk list.

        Used for the ``hybrid`` corpus (raw + wiki chunks fused into one
        retriever). No fallback: the chunk list is authoritative. ``wiki_page_ids``
        is threaded to the retrieval service so synthesis questions can boost the
        fused corpus's wiki hits.
        """

        repo = cls(allow_fallback=False)
        repo._chunks = list(chunks)
        repo._documents = cls._documents_from_chunks(repo._chunks)
        repo._retrieval_service = RetrievalService(repo._chunks, wiki_page_ids=wiki_page_ids)
        repo._source = source
        return repo

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
# Singleton + retrieval-source routing (Phase 10C)
# ---------------------------------------------------------------------------
#
# ``get_repository()`` is the accessor the LangGraph retriever nodes call. It
# routes to the raw / wiki / hybrid corpus based on a ContextVar that
# ``run_query`` sets per request. This keeps the graph nodes byte-identical
# (the design's "unchanged nodes"): the only in-request callers of
# ``get_repository`` are the two retriever nodes, and the ContextVar defaults
# to the configured source (raw) everywhere else.

_RETRIEVAL_SOURCES = ("raw", "wiki", "hybrid")

_repository_singleton: DocumentRepository | None = None
_wiki_repository_singleton: DocumentRepository | None = None
_hybrid_repository_singleton: DocumentRepository | None = None
_singleton_lock = Lock()

_active_retrieval_source: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "trustrag_retrieval_source", default=None
)


def _normalize_source(source: str | None) -> str:
    src = (source or "").strip().lower()
    return src if src in _RETRIEVAL_SOURCES else "raw"


def resolve_retrieval_source() -> str:
    """The active retrieval source: request override, else the configured default."""

    override = _active_retrieval_source.get()
    if override is not None:
        return _normalize_source(override)
    from ..core.config import get_settings

    return _normalize_source(getattr(get_settings(), "retrieval_source", "raw"))


class use_retrieval_source:
    """Context manager scoping the active retrieval source to a request."""

    def __init__(self, source: str | None) -> None:
        self._source = source
        self._token: contextvars.Token | None = None

    def __enter__(self) -> str:
        resolved = _normalize_source(source=self._source) if self._source else None
        self._token = _active_retrieval_source.set(resolved)
        return resolve_retrieval_source()

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _active_retrieval_source.reset(self._token)


# ---------------------------------------------------------------------------
# Per-request tenant repository binding (Phase 11 — RAG path tenant scoping)
# ---------------------------------------------------------------------------
#
# The retriever nodes resolve their corpus through ``get_repository()``. For the
# raw source that is normally the process-global raw store, which is tenant-blind.
# In multi-tenant (postgres) mode the ``/v1/rag/query`` route binds the request's
# tenant-scoped catalog here (via ``run_query(catalog=...)``) so retrieval reads
# only that tenant's documents — the RAG analogue of ``/v1/documents`` calling
# ``container.catalog_for(tenant_id)``. The wiki / hybrid corpora are derived
# global stores and are intentionally left unscoped by this binding.

_active_tenant_repository: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "trustrag_tenant_repository", default=None
)


class use_tenant_repository:
    """Context manager binding the active tenant-scoped repository (raw source).

    ``repository`` is any object exposing ``get_retrieval_service()`` — a
    :class:`DocumentRepository` in local mode, a ``PostgresDocumentCatalog`` in
    postgres mode. ``None`` is a no-op, so ``get_repository()`` falls back to the
    global raw store and every existing ``run_query`` caller is unchanged.
    """

    def __init__(self, repository: Any | None) -> None:
        self._repository = repository
        self._token: contextvars.Token | None = None

    def __enter__(self) -> Any | None:
        self._token = _active_tenant_repository.set(self._repository)
        return self._repository

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _active_tenant_repository.reset(self._token)


def get_raw_repository() -> DocumentRepository:
    """The raw-corpus repository — always the raw store, regardless of source."""

    global _repository_singleton
    with _singleton_lock:
        if _repository_singleton is None:
            _repository_singleton = DocumentRepository()
        return _repository_singleton


def get_wiki_repository() -> DocumentRepository:
    """The wiki-corpus repository over the derived wiki chunk store (no fallback)."""

    global _wiki_repository_singleton
    with _singleton_lock:
        if _wiki_repository_singleton is None:
            _wiki_repository_singleton = DocumentRepository(
                chunk_store_path=_DEFAULT_WIKI_CHUNK_STORE,
                document_store_path=_NO_STORE,
                sample_dir=_NO_STORE,
                allow_fallback=False,
            )
        return _wiki_repository_singleton


def raw_document_ids() -> set[str]:
    """The raw corpus's document ids — the grounding set for wiki citations."""

    return {d.document_id for d in get_raw_repository().load_documents()}


def wiki_page_source_map() -> dict[str, list[str]]:
    """``wiki_page_id -> underlying raw doc_ids`` from the wiki corpus.

    Built from the loaded wiki chunk metadata (page_id + the page's ``sources``).
    Used at the query boundary to resolve wiki page identity back to the raw
    documents it compiles (two-layer citations + wiki-mode eval).
    """

    mapping: dict[str, list[str]] = {}
    for chunk in get_wiki_repository().load_chunks():
        page_id = chunk.metadata.get("page_id")
        if page_id and page_id not in mapping:
            mapping[page_id] = list(chunk.metadata.get("sources") or [])
    return mapping


def get_hybrid_repository() -> DocumentRepository:
    """Raw + wiki chunks fused into one retriever (Phase 10C hybrid corpus)."""

    global _hybrid_repository_singleton
    if _hybrid_repository_singleton is not None:
        return _hybrid_repository_singleton
    # Fetch the source corpora *before* taking the singleton lock — each of
    # get_wiki/get_raw acquires it, and it is not reentrant.
    wiki_chunks = get_wiki_repository().load_chunks()
    raw_chunks = get_raw_repository().load_chunks()
    with _singleton_lock:
        if _hybrid_repository_singleton is None:
            _hybrid_repository_singleton = DocumentRepository.from_chunks(
                raw_chunks + wiki_chunks,
                source="hybrid:raw+wiki",
                wiki_page_ids={c.document_id for c in wiki_chunks},
            )
        return _hybrid_repository_singleton


def get_repository() -> DocumentRepository:
    """Return the repository for the active retrieval source (raw by default).

    For the raw source a per-request tenant repository (bound by the RAG route
    via :class:`use_tenant_repository`) takes precedence, so retrieval is scoped
    to the caller's tenant instead of the process-global raw store. The wiki /
    hybrid corpora are derived global stores and ignore the binding.
    """

    source = resolve_retrieval_source()
    if source == "wiki":
        return get_wiki_repository()
    if source == "hybrid":
        return get_hybrid_repository()
    tenant_repository = _active_tenant_repository.get()
    if tenant_repository is not None:
        return tenant_repository
    return get_raw_repository()


def reset_repository() -> None:
    """Reset all corpus singletons — used by tests that want a clean load."""

    global _repository_singleton, _wiki_repository_singleton, _hybrid_repository_singleton
    with _singleton_lock:
        _repository_singleton = None
        _wiki_repository_singleton = None
        _hybrid_repository_singleton = None
