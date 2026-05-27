"""TrustRAG ingestion package.

Phase 2B: load Markdown / PDF / DOCX with sidecar YAML metadata into
``AccountingDocument`` records and split them into ``DocumentChunk``
records consumed by downstream services.
"""

from .chunker import chunk_document, chunk_documents
from .docx_loader import load_docx_document
from .frontmatter import (
    FrontMatterError,
    MissingFrontMatterError,
    parse_frontmatter_markdown,
)
from .markdown_loader import load_markdown_document, load_markdown_documents
from .models import AccountingDocument, DocumentChunk, compute_checksum, make_chunk_id
from .pdf_loader import load_pdf_document
from .sidecar import load_sidecar_metadata, sidecar_path
from .store_writer import write_chunk_store, write_document_store
from .unified_loader import load_documents_from_directory

__all__ = [
    "AccountingDocument",
    "DocumentChunk",
    "FrontMatterError",
    "MissingFrontMatterError",
    "chunk_document",
    "chunk_documents",
    "compute_checksum",
    "load_docx_document",
    "load_documents_from_directory",
    "load_markdown_document",
    "load_markdown_documents",
    "load_pdf_document",
    "load_sidecar_metadata",
    "make_chunk_id",
    "parse_frontmatter_markdown",
    "sidecar_path",
    "write_chunk_store",
    "write_document_store",
]
