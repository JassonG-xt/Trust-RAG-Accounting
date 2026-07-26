"""Tenant-scoped Postgres document catalog over the active index generation."""

from __future__ import annotations

from threading import Lock
from typing import Any

from sqlalchemy import Engine, and_, select

from ..core.config import Settings
from ..embeddings import EmbeddingProvider
from ..ingestion import AccountingDocument, DocumentChunk
from ..retrieval import RetrievalService
from ..services.document_repository import (
    _is_malicious_query,
    _scored_chunk_to_evidence_dict,
)
from ..vectorstore import VectorStore
from .schema import document_chunks, index_generations


class PostgresDocumentCatalog:
    """Deep read module that hot-swaps when active generation changes."""

    def __init__(
        self,
        engine: Engine,
        *,
        tenant_id: str,
        settings: Settings,
        embedding_provider: EmbeddingProvider | None = None,
        vector_store: VectorStore | None = None,
    ) -> None:
        self._engine = engine
        self._tenant_id = tenant_id
        self._settings = settings
        self._embedding_provider = embedding_provider
        self._vector_store = vector_store
        self._generation_id: str | None = None
        self._documents: list[AccountingDocument] = []
        self._chunks: list[DocumentChunk] = []
        self._retrieval: RetrievalService | None = None
        self._lock = Lock()

    @property
    def source(self) -> str:
        self._ensure_loaded()
        generation = self._generation_id or "none"
        return f"postgres:{self._tenant_id}:{generation}"

    def load_documents(self) -> list[AccountingDocument]:
        self._ensure_loaded()
        return list(self._documents)

    def load_chunks(self) -> list[DocumentChunk]:
        self._ensure_loaded()
        return list(self._chunks)

    def describe(self) -> list[dict]:
        return [
            {
                "document_id": document.document_id,
                "title": document.title,
                "version": document.version,
                "document_type": document.document_type,
                "client": document.client,
                "valid_from": document.valid_from,
                "valid_to": document.valid_to,
                "policy_family": document.policy_family,
                "replaces": document.replaces,
                "is_malicious": document.is_malicious,
                "source_path": document.source_path,
            }
            for document in self.load_documents()
        ]

    def chunk_count(self) -> int:
        return len(self.load_chunks())

    def get_retrieval_service(self) -> RetrievalService:
        """Retrieval seam consumed by the RAG graph nodes.

        Mirrors :meth:`DocumentRepository.get_retrieval_service` so a
        tenant-scoped catalog is a drop-in for the process-global repository at
        the ``support_retriever`` / ``counter_retriever`` call site.
        """

        self._ensure_loaded()
        assert self._retrieval is not None
        return self._retrieval

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
        self._ensure_loaded()
        if not include_malicious and _is_malicious_query(question):
            include_malicious = True
        assert self._retrieval is not None
        scored = self._retrieval.search(
            question,
            question_type=question_type,
            top_k=top_k if top_k is not None else limit,
            stance=stance,
            include_malicious=include_malicious,
        )
        if client is not None:
            scored = [item for item in scored if item.client in {None, client}]
        return [
            _scored_chunk_to_evidence_dict(item, stance=stance) for item in scored
        ]

    def _ensure_loaded(self) -> None:
        active_generation = self._active_generation_id()
        if self._retrieval is not None and active_generation == self._generation_id:
            return
        with self._lock:
            active_generation = self._active_generation_id()
            if self._retrieval is not None and active_generation == self._generation_id:
                return
            self._generation_id = active_generation
            self._chunks = self._load_chunks_from_database(active_generation)
            self._documents = self._documents_from_chunks(self._chunks)
            self._retrieval = RetrievalService(
                self._chunks,
                settings=self._settings,
                embedding_provider=self._embedding_provider,
                vector_store=self._vector_store,
                secure_payload_filter={
                    "tenant_id": self._tenant_id,
                    "generation_id": active_generation or "none",
                },
                index_vectors=False,
            )

    def _active_generation_id(self) -> str | None:
        with self._engine.connect() as connection:
            return connection.execute(
                select(index_generations.c.generation_id)
                .where(
                    and_(
                        index_generations.c.tenant_id == self._tenant_id,
                        index_generations.c.status == "active",
                    )
                )
                .order_by(
                    index_generations.c.activated_at.desc(),
                    index_generations.c.generation_id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()

    @staticmethod
    def _documents_from_chunks(
        chunks: list[DocumentChunk],
    ) -> list[AccountingDocument]:
        documents_by_id: dict[str, AccountingDocument] = {}
        for chunk in chunks:
            if chunk.document_id in documents_by_id:
                continue
            documents_by_id[chunk.document_id] = AccountingDocument(
                document_id=chunk.document_id,
                title=chunk.title,
                version=chunk.version,
                valid_from=chunk.valid_from,
                valid_to=chunk.valid_to,
                document_type=chunk.document_type,
                client=chunk.client,
                policy_family=chunk.policy_family,
                replaces=chunk.replaces,
                risk_type=chunk.risk_type,
                is_malicious=chunk.is_malicious,
                source_path=chunk.source_path,
                content="",
                checksum=chunk.checksum,
                metadata=dict(chunk.metadata),
            )
        return [documents_by_id[key] for key in sorted(documents_by_id)]

    def _load_chunks_from_database(
        self,
        generation_id: str | None,
    ) -> list[DocumentChunk]:
        if generation_id is None:
            return []
        statement = (
            select(document_chunks.c.metadata_json, document_chunks.c.content)
            .where(
                and_(
                    document_chunks.c.tenant_id == self._tenant_id,
                    document_chunks.c.generation_id == generation_id,
                )
            )
            .order_by(document_chunks.c.document_id, document_chunks.c.position)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).all()
            return [
                DocumentChunk.model_validate({**dict(metadata), "content": content})
                for metadata, content in rows
            ]
