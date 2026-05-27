"""Markdown loader — translates YAML-front-matter Markdown files into
``AccountingDocument`` instances.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .frontmatter import FrontMatterError, parse_frontmatter_markdown
from .models import AccountingDocument, compute_checksum

logger = logging.getLogger(__name__)


_REQUIRED_FIELDS = ("title", "version", "document_type")


def _coerce_str(value) -> str | None:
    """Normalize YAML-decoded values to ISO-style strings or None."""

    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _derive_policy_family(metadata: dict, source_path: Path) -> str | None:
    if metadata.get("policy_family"):
        return str(metadata["policy_family"])
    document_type = metadata.get("document_type")
    if isinstance(document_type, str) and document_type:
        return document_type
    # Fallback: filename stem minus the trailing _YYYY[...] suffix.
    stem = source_path.stem
    parts = stem.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return stem


def _derive_document_id(metadata: dict, source_path: Path) -> str:
    if metadata.get("document_id"):
        return str(metadata["document_id"])
    return source_path.stem


def load_markdown_document(path: Path) -> AccountingDocument:
    """Load and validate a single Markdown file."""

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"file not found: {path}")
    if path.suffix.lower() != ".md":
        raise ValueError(f"not a markdown file: {path}")

    raw = path.read_text(encoding="utf-8")
    try:
        metadata, body = parse_frontmatter_markdown(raw)
    except FrontMatterError as exc:
        raise FrontMatterError(f"{path}: {exc}") from exc

    missing = [f for f in _REQUIRED_FIELDS if not metadata.get(f)]
    if missing:
        raise FrontMatterError(
            f"{path}: missing required front-matter field(s): {missing}"
        )

    document_id = _derive_document_id(metadata, path)
    policy_family = _derive_policy_family(metadata, path)

    # Truthiness for booleans coming from YAML.
    is_malicious = bool(metadata.get("is_malicious", False))

    # Canonical metadata for checksum — only the meaningful fields, sorted.
    canonical_metadata = {
        "document_id": document_id,
        "title": metadata.get("title"),
        "version": metadata.get("version"),
        "valid_from": metadata.get("valid_from"),
        "valid_to": metadata.get("valid_to"),
        "document_type": metadata.get("document_type"),
        "client": metadata.get("client"),
        "policy_family": policy_family,
        "replaces": metadata.get("replaces"),
        "risk_type": metadata.get("risk_type"),
        "is_malicious": is_malicious,
    }

    checksum = compute_checksum(body, canonical_metadata)

    extra_metadata = {
        k: v
        for k, v in metadata.items()
        if k not in canonical_metadata
    }
    extra_metadata["source_format"] = "markdown"

    return AccountingDocument(
        document_id=document_id,
        title=str(metadata.get("title")),
        version=str(metadata.get("version")),
        valid_from=_coerce_str(metadata.get("valid_from")),
        valid_to=_coerce_str(metadata.get("valid_to")),
        document_type=str(metadata.get("document_type")),
        client=_coerce_str(metadata.get("client")),
        policy_family=policy_family,
        replaces=_coerce_str(metadata.get("replaces")),
        risk_type=_coerce_str(metadata.get("risk_type")),
        is_malicious=is_malicious,
        source_path=str(path),
        content=body,
        checksum=checksum,
        metadata=extra_metadata,
    )


def load_markdown_documents(directory: Path) -> list[AccountingDocument]:
    """Load every Markdown file under ``directory`` (non-recursive)."""

    directory = Path(directory)
    if not directory.exists():
        raise FileNotFoundError(f"directory not found: {directory}")
    if not directory.is_dir():
        raise NotADirectoryError(f"not a directory: {directory}")

    documents: list[AccountingDocument] = []
    md_paths = sorted(p for p in directory.iterdir() if p.suffix.lower() == ".md")
    for path in md_paths:
        try:
            documents.append(load_markdown_document(path))
        except FrontMatterError:
            # Re-raise so the CLI can surface the file path clearly.
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("failed to load %s: %s", path, exc)
            raise

    return documents
