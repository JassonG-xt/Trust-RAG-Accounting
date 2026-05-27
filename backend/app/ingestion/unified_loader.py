"""Unified directory loader — Markdown + PDF + DOCX → AccountingDocument."""

from __future__ import annotations

import logging
from pathlib import Path

from .docx_loader import load_docx_document
from .markdown_loader import load_markdown_document
from .models import AccountingDocument
from .pdf_loader import load_pdf_document

logger = logging.getLogger(__name__)


_SUPPORTED_SUFFIXES = {".md", ".pdf", ".docx"}
# Filenames we deliberately skip (sidecar metadata, hidden files).
_IGNORED_SUFFIXES_OR_NAMES = {".metadata.yaml", ".metadata.yml"}


def _is_ignored(path: Path) -> bool:
    name = path.name
    if name.startswith("."):
        return True
    # ".metadata.yaml" — anything ending in this sidecar pattern is sidecar
    # metadata for some other source file, not its own document.
    return any(name.endswith(suffix) for suffix in _IGNORED_SUFFIXES_OR_NAMES)


def _load_single(path: Path) -> AccountingDocument:
    suffix = path.suffix.lower()
    if suffix == ".md":
        return load_markdown_document(path)
    if suffix == ".pdf":
        return load_pdf_document(path)
    if suffix == ".docx":
        return load_docx_document(path)
    raise ValueError(f"unsupported file type: {path}")


def load_documents_from_directory(directory: Path) -> list[AccountingDocument]:
    """Load every supported file under ``directory`` (non-recursive)."""

    directory = Path(directory)
    if not directory.exists():
        raise FileNotFoundError(f"directory not found: {directory}")
    if not directory.is_dir():
        raise NotADirectoryError(f"not a directory: {directory}")

    candidates = sorted(
        p
        for p in directory.iterdir()
        if p.is_file()
        and not _is_ignored(p)
        and p.suffix.lower() in _SUPPORTED_SUFFIXES
    )

    documents: list[AccountingDocument] = []
    for path in candidates:
        try:
            documents.append(_load_single(path))
        except Exception as exc:
            raise type(exc)(f"{path}: {exc}") from exc
    return documents
