"""TrustRAG ingestion package.

Phase 2A: parse YAML-front-matter Markdown documents from
``sample_docs/`` (or any compatible source) into
``AccountingDocument`` records that downstream services consume.
"""

from .frontmatter import (
    FrontMatterError,
    MissingFrontMatterError,
    parse_frontmatter_markdown,
)
from .markdown_loader import load_markdown_document, load_markdown_documents
from .models import AccountingDocument

__all__ = [
    "AccountingDocument",
    "FrontMatterError",
    "MissingFrontMatterError",
    "load_markdown_document",
    "load_markdown_documents",
    "parse_frontmatter_markdown",
]
