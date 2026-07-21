"""Deterministic mock ingest for Phase 10A.

``WIKI_INGEST_MODE=mock`` (the default) replays a pre-authored
:class:`WikiUpdateProposal` fixture for a source document instead of calling a
real LLM. This is the only ingest path that exists in Phase 10A; the bounded
tool-calling agent behind ``WIKI_INGEST_MODE=llm`` lands in Phase 10B and is
intentionally not implemented here.

The returned proposal is *staged* — it is never applied by this function. A
caller (a test-driven approval in 10A, the review queue in 10B) decides whether
to hand it to :func:`backend.app.wiki.apply.apply_proposal`.
"""

from __future__ import annotations

from pathlib import Path

from ..core.config import get_settings
from .models import WikiUpdateProposal


def mock_ingest(source_doc_id: str, *, proposals_dir: Path | str) -> WikiUpdateProposal:
    """Load and return the staged fixture proposal for ``source_doc_id``.

    Raises ``NotImplementedError`` if ingest mode is not ``mock`` (real ingest
    is Phase 10B) and ``FileNotFoundError`` if no fixture proposal exists.
    """

    mode = get_settings().wiki_ingest_mode
    if mode != "mock":
        raise NotImplementedError(
            f"wiki ingest mode {mode!r} is not available in Phase 10A; "
            "only 'mock' (fixture replay) is implemented"
        )
    path = Path(proposals_dir) / f"{source_doc_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no mock proposal fixture for '{source_doc_id}': {path}")
    return WikiUpdateProposal.model_validate_json(path.read_text(encoding="utf-8"))
