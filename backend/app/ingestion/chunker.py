"""Deterministic document chunker.

Strategy (intentionally simple — Phase 3 swaps in a semantic splitter):

* **Markdown**: split on ATX headings (``#`` / ``##`` / ``###`` ...).
  Each heading + its body becomes a section. The heading is preserved
  inside the chunk content; ``section_title`` exposes it as metadata.
  Sections longer than ``max_chars`` are further split by a sliding
  window with character-level overlap.
* **Non-markdown** (PDF / DOCX / plain text): split by blank-line
  paragraphs first, then sliding-window any oversize paragraphs.

Guarantees:

* Chunk order is stable across runs (no hashing of mutable state).
* No chunk is empty or whitespace-only.
* The overlap window can never produce zero progress — if the next
  start would be ≤ the current start, we advance to the chunk end.
"""

from __future__ import annotations

import re

from .models import (
    AccountingDocument,
    DocumentChunk,
    compute_checksum,
    estimate_tokens,
    make_chunk_id,
)

# ATX heading regex: 1-6 ``#`` followed by whitespace.
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")

# Paragraph separator: 2+ newlines.
_PARAGRAPH_SEP = re.compile(r"\n\s*\n")

# Sentence splitter used only inside oversize paragraphs. It deliberately
# stays simple and deterministic: sentence-ending punctuation wins, and
# punctuation-free text falls back to the character window.
_SENTENCE_PATTERN = re.compile(r"[^.!?。！？]+[.!?。！？]+|[^.!?。！？]+$")


def _is_markdown(document: AccountingDocument) -> bool:
    fmt = document.metadata.get("source_format")
    if fmt == "markdown":
        return True
    if fmt:
        return False
    # Fall back to suffix.
    return document.source_path.lower().endswith(".md")


# ---------------------------------------------------------------------------
# Section splitting
# ---------------------------------------------------------------------------


def _split_markdown_sections(text: str) -> list[tuple[str | None, str]]:
    """Return ordered ``(section_title, section_body)`` pairs.

    The section_title is the most recent heading; section_body
    INCLUDES the heading line so the chunk content remains
    self-explanatory.
    """

    lines = text.splitlines()
    sections: list[tuple[str | None, list[str]]] = []
    current_title: str | None = None
    current_lines: list[str] = []

    for line in lines:
        heading = _HEADING_PATTERN.match(line)
        if heading:
            # Flush previous section (if it had any content).
            if current_lines:
                sections.append((current_title, current_lines))
                current_lines = []
            current_title = heading.group(2).strip()
        current_lines.append(line)

    if current_lines:
        sections.append((current_title, current_lines))

    out: list[tuple[str | None, str]] = []
    for title, ls in sections:
        body = "\n".join(ls).strip()
        if body:
            out.append((title, body))
    return out


def _split_plain_paragraphs(text: str) -> list[tuple[str | None, str]]:
    paragraphs = [p.strip() for p in _PARAGRAPH_SEP.split(text)]
    return [(None, p) for p in paragraphs if p]


def _split_paragraph_text(text: str) -> list[str]:
    return [p.strip() for p in _PARAGRAPH_SEP.split(text) if p.strip()]


def _split_sentences(text: str) -> list[str]:
    return [m.group(0).strip() for m in _SENTENCE_PATTERN.finditer(text) if m.group(0).strip()]


# ---------------------------------------------------------------------------
# Window splitting (applied per section / per paragraph)
# ---------------------------------------------------------------------------


def _window_split(text: str, *, max_chars: int, overlap_chars: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    step = max(max_chars - overlap_chars, 1)
    out: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + max_chars, n)
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        next_start = start + step
        if next_start <= start:
            # Defensive — should never trigger because step >= 1.
            next_start = end
        start = next_start
    return out


def _overlap_units(
    units: list[str],
    *,
    joiner: str,
    overlap_chars: int,
) -> list[str]:
    if overlap_chars <= 0:
        return []

    out: list[str] = []
    for unit in reversed(units):
        candidate = [unit, *out]
        text = joiner.join(candidate)
        if len(text) > overlap_chars:
            break
        out = candidate
    return out


def _split_units(
    units: list[str],
    *,
    joiner: str,
    max_chars: int,
    overlap_chars: int,
    split_oversize,
) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            chunk = joiner.join(current).strip()
            if chunk:
                chunks.append(chunk)

    for unit in units:
        if len(unit) > max_chars:
            pieces = split_oversize(unit)
            if current and pieces:
                first_candidate = joiner.join([*current, pieces[0]]).strip()
                if len(first_candidate) <= max_chars:
                    chunks.append(first_candidate)
                    chunks.extend(pieces[1:])
                    current = []
                    continue
            flush()
            chunks.extend(pieces)
            current = []
            continue

        candidate_units = [*current, unit]
        candidate = joiner.join(candidate_units).strip()
        if not current or len(candidate) <= max_chars:
            current = candidate_units
            continue

        flush()
        current = _overlap_units(
            current,
            joiner=joiner,
            overlap_chars=overlap_chars,
        )
        while current and len(joiner.join([*current, unit]).strip()) > max_chars:
            current = current[1:]
        current.append(unit)

    flush()
    return chunks


def _semantic_split(text: str, *, max_chars: int, overlap_chars: int) -> list[str]:
    """Split oversize text on paragraph/sentence boundaries before windows."""

    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    paragraphs = _split_paragraph_text(text)
    if len(paragraphs) > 1:
        return _split_units(
            paragraphs,
            joiner="\n\n",
            max_chars=max_chars,
            overlap_chars=overlap_chars,
            split_oversize=lambda paragraph: _semantic_split(
                paragraph,
                max_chars=max_chars,
                overlap_chars=overlap_chars,
            ),
        )

    sentences = _split_sentences(text)
    if len(sentences) > 1:
        return _split_units(
            sentences,
            joiner=" ",
            max_chars=max_chars,
            overlap_chars=overlap_chars,
            split_oversize=lambda sentence: _window_split(
                sentence,
                max_chars=max_chars,
                overlap_chars=overlap_chars,
            ),
        )

    return _window_split(text, max_chars=max_chars, overlap_chars=overlap_chars)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def chunk_document(
    document: AccountingDocument,
    *,
    max_chars: int = 900,
    overlap_chars: int = 120,
) -> list[DocumentChunk]:
    """Split ``document.content`` into ordered :class:`DocumentChunk` records."""

    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError(
            f"overlap_chars must satisfy 0 <= overlap_chars < max_chars; "
            f"got overlap_chars={overlap_chars}, max_chars={max_chars}"
        )

    content = (document.content or "").strip()
    if not content:
        # Nothing to chunk — return an empty list. Callers decide whether
        # that's an error for their format.
        return []

    if _is_markdown(document):
        sections = _split_markdown_sections(content)
    else:
        sections = _split_plain_paragraphs(content)

    if not sections:
        # Fall back to whole content as a single chunk.
        sections = [(None, content)]

    chunks: list[DocumentChunk] = []
    index = 0
    for section_title, body in sections:
        pieces = _semantic_split(body, max_chars=max_chars, overlap_chars=overlap_chars)
        for piece in pieces:
            metadata = {
                "section_title": section_title,
                "chunk_index": index,
                "source_format": document.metadata.get("source_format"),
                "document_id": document.document_id,
            }
            chunk_id = make_chunk_id(document.document_id, index)
            chunks.append(
                DocumentChunk(
                    chunk_id=chunk_id,
                    document_id=document.document_id,
                    title=document.title,
                    version=document.version,
                    document_type=document.document_type,
                    client=document.client,
                    policy_family=document.policy_family,
                    replaces=document.replaces,
                    valid_from=document.valid_from,
                    valid_to=document.valid_to,
                    chunk_index=index,
                    section_title=section_title,
                    page_number=None,
                    content=piece,
                    token_estimate=estimate_tokens(piece),
                    source_path=document.source_path,
                    checksum=compute_checksum(piece, metadata),
                    risk_type=document.risk_type,
                    is_malicious=document.is_malicious,
                    metadata={},
                )
            )
            index += 1

    return chunks


def chunk_documents(
    documents: list[AccountingDocument],
    **chunk_kwargs,
) -> list[DocumentChunk]:
    """Chunk a batch; preserves the order of ``documents``."""

    out: list[DocumentChunk] = []
    for doc in documents:
        out.extend(chunk_document(doc, **chunk_kwargs))
    return out
