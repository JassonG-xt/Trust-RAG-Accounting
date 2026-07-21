"""Phase 10A wiki compilation layer.

The wiki is a persistent, LLM-owned markdown tree compiled from the immutable
raw corpus. Phase 10A ships the data model, markdown store, index/log
maintenance, the sole-writer applier, tier-1 lint, and a deterministic mock
ingest — all offline, with every write routed through an explicit approval.
The real review-queue wiring and the LLM ingest agent land in Phase 10B.
"""

from __future__ import annotations

from .apply import apply_proposal
from .lint import LintFinding, LintReport, lint_wiki
from .mock_ingest import mock_ingest
from .models import (
    AnalysisResult,
    ApplyResult,
    PagePatch,
    WikiFrontmatter,
    WikiPage,
    WikiUpdateProposal,
)
from .store import load_wiki, parse_page, refresh_wiki_stores, render_markdown

__all__ = [
    "AnalysisResult",
    "ApplyResult",
    "LintFinding",
    "LintReport",
    "PagePatch",
    "WikiFrontmatter",
    "WikiPage",
    "WikiUpdateProposal",
    "apply_proposal",
    "lint_wiki",
    "load_wiki",
    "mock_ingest",
    "parse_page",
    "refresh_wiki_stores",
    "render_markdown",
]
