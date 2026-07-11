from __future__ import annotations

import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from sqlalchemy import Engine, and_, delete, insert, select, update

from ..embeddings import EmbeddingProvider
from ..ingestion import AccountingDocument, DocumentChunk, chunk_documents
from ..ingestion.unified_loader import load_documents_from_directory
from ..persistence import SourceObjectStore
from ..persistence.schema import (
    document_chunks,
    document_versions,
    documents,
    index_generations,
)
from ..retrieval.vector_retriever import _chunk_payload, _chunk_searchable_text
from ..vectorstore import VectorRecord, VectorStore
from .coordinator import IndexBuildResult
from .models import IndexJob


class ProductionDocumentIndexer:
    """Build a complete staging corpus from the current active generation."""

    def __init__(
        self,
        engine: Engine,
        *,
        tenant_id: str,
        source_store: SourceObjectStore,
        embedding_provider: EmbeddingProvider,
        vector_store: VectorStore,
    ) -> None:
        self._engine = engine
        self._tenant_id = tenant_id
        self._source_store = source_store
        self._embedding_provider = embedding_provider
        self._vector_store = vector_store

    def build(self, job: IndexJob, generation_id: str) -> IndexBuildResult:
        chunks = self._load_active_chunks()
        if job.operation == "upsert":
            document = self._load_uploaded_document(job)
            new_chunks = chunk_documents([document])
            chunks = [chunk for chunk in chunks if chunk.document_id != document.document_id]
            chunks.extend(new_chunks)
            self._upsert_document(document, source_uri=job.source_uri or "")
        elif job.operation == "delete":
            if not job.document_id:
                raise ValueError("delete index job requires document_id")
            chunks = [chunk for chunk in chunks if chunk.document_id != job.document_id]
            self._tombstone_document(job.document_id)
        elif job.operation not in {"reindex", "reconcile"}:
            raise ValueError(f"unsupported index operation: {job.operation!r}")

        chunks.sort(key=lambda item: (item.document_id, item.chunk_index))
        self._write_generation(generation_id, chunks)
        self._write_vectors(generation_id, chunks)
        catalog_count = self._catalog_count(generation_id)
        vector_count = self._vector_store.count(
            {"tenant_id": self._tenant_id, "generation_id": generation_id}
        )
        return IndexBuildResult(
            catalog_count=catalog_count,
            vector_count=vector_count,
            lexical_count=len(chunks),
        )

    def _load_uploaded_document(self, job: IndexJob) -> AccountingDocument:
        if not job.source_uri:
            raise ValueError("upsert index job requires source_uri")
        filename = Path(str(job.payload.get("filename") or "")).name
        if Path(filename).suffix.lower() not in {".md", ".pdf", ".docx"}:
            raise ValueError("index job filename has unsupported format")
        content = self._source_store.get(job.source_uri)
        with TemporaryDirectory(prefix="trustrag-index-") as directory:
            source_path = Path(directory) / filename
            source_path.write_bytes(content)
            metadata = job.payload.get("metadata") or {}
            if source_path.suffix.lower() in {".pdf", ".docx"}:
                sidecar = source_path.with_name(source_path.stem + ".metadata.yaml")
                sidecar.write_text(
                    yaml.safe_dump(metadata, allow_unicode=True, sort_keys=True),
                    encoding="utf-8",
                )
            loaded = load_documents_from_directory(Path(directory))
        if len(loaded) != 1:
            raise ValueError("index job must resolve to exactly one document")
        return loaded[0]

    def _load_active_chunks(self) -> list[DocumentChunk]:
        with self._engine.connect() as connection:
            active = connection.execute(
                select(index_generations.c.generation_id)
                .where(
                    and_(
                        index_generations.c.tenant_id == self._tenant_id,
                        index_generations.c.status == "active",
                    )
                )
                .order_by(index_generations.c.activated_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if active is None:
                return []
            rows = connection.execute(
                select(document_chunks.c.metadata_json, document_chunks.c.content)
                .where(
                    and_(
                        document_chunks.c.tenant_id == self._tenant_id,
                        document_chunks.c.generation_id == active,
                    )
                )
                .order_by(document_chunks.c.document_id, document_chunks.c.position)
            ).all()
        return [
            DocumentChunk.model_validate({**dict(metadata), "content": content})
            for metadata, content in rows
        ]

    def _upsert_document(self, document: AccountingDocument, *, source_uri: str) -> None:
        version_id = f"{document.document_id}:{document.checksum}"
        metadata = document.model_dump(mode="json", exclude={"content"})
        with self._engine.begin() as connection:
            exists = connection.execute(
                select(documents.c.document_id).where(
                    and_(
                        documents.c.tenant_id == self._tenant_id,
                        documents.c.document_id == document.document_id,
                    )
                )
            ).scalar_one_or_none()
            values = {
                "current_version_id": version_id,
                "title": document.title,
                "document_type": document.document_type,
                "client": document.client,
                "tombstoned": False,
                "metadata_json": metadata,
                "updated_at": document.ingested_at,
            }
            if exists is None:
                connection.execute(
                    insert(documents).values(
                        tenant_id=self._tenant_id,
                        document_id=document.document_id,
                        created_at=document.ingested_at,
                        **values,
                    )
                )
            else:
                connection.execute(
                    update(documents)
                    .where(
                        and_(
                            documents.c.tenant_id == self._tenant_id,
                            documents.c.document_id == document.document_id,
                        )
                    )
                    .values(**values)
                )
            version_exists = connection.execute(
                select(document_versions.c.version_id).where(
                    and_(
                        document_versions.c.tenant_id == self._tenant_id,
                        document_versions.c.version_id == version_id,
                    )
                )
            ).scalar_one_or_none()
            if version_exists is None:
                connection.execute(
                    insert(document_versions).values(
                        tenant_id=self._tenant_id,
                        version_id=version_id,
                        document_id=document.document_id,
                        version_label=document.version,
                        checksum=document.checksum,
                        source_uri=source_uri,
                        parse_status="succeeded",
                        metadata_json=metadata,
                        created_at=document.ingested_at,
                    )
                )

    def _tombstone_document(self, document_id: str) -> None:
        with self._engine.begin() as connection:
            result = connection.execute(
                update(documents)
                .where(
                    and_(
                        documents.c.tenant_id == self._tenant_id,
                        documents.c.document_id == document_id,
                    )
                )
                .values(tombstoned=True)
            )
        if not result.rowcount:
            raise KeyError(f"document {document_id!r} not found")

    def _write_generation(
        self,
        generation_id: str,
        chunks: list[DocumentChunk],
    ) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                delete(document_chunks).where(
                    and_(
                        document_chunks.c.tenant_id == self._tenant_id,
                        document_chunks.c.generation_id == generation_id,
                    )
                )
            )
            for chunk in chunks:
                version_id = connection.execute(
                    select(documents.c.current_version_id).where(
                        and_(
                            documents.c.tenant_id == self._tenant_id,
                            documents.c.document_id == chunk.document_id,
                        )
                    )
                ).scalar_one()
                connection.execute(
                    insert(document_chunks).values(
                        tenant_id=self._tenant_id,
                        generation_id=generation_id,
                        chunk_id=chunk.chunk_id,
                        document_id=chunk.document_id,
                        version_id=version_id,
                        position=chunk.chunk_index,
                        checksum=chunk.checksum,
                        content=chunk.content,
                        metadata_json=chunk.model_dump(
                            mode="json", exclude={"content"}
                        ),
                    )
                )

    def _write_vectors(self, generation_id: str, chunks: list[DocumentChunk]) -> None:
        if not chunks:
            return
        vectors = self._embedding_provider.embed_texts(
            [_chunk_searchable_text(chunk) for chunk in chunks]
        )
        records = [
            VectorRecord(
                id=str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"trustrag:{self._tenant_id}:{generation_id}:{chunk.chunk_id}",
                    )
                ),
                vector=vector,
                payload={
                    **_chunk_payload(chunk),
                    "tenant_id": self._tenant_id,
                    "generation_id": generation_id,
                },
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        self._vector_store.upsert(records)

    def _catalog_count(self, generation_id: str) -> int:
        with self._engine.connect() as connection:
            return len(
                connection.execute(
                    select(document_chunks.c.chunk_id).where(
                        and_(
                            document_chunks.c.tenant_id == self._tenant_id,
                            document_chunks.c.generation_id == generation_id,
                        )
                    )
                ).all()
            )
