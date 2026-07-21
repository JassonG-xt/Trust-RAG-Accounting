"""Shared fixtures/metadata for the Phase 10A wiki tests.

Not collected by pytest (leading underscore). ``DOC_CLIENTS`` mirrors the
``document_id`` / ``client`` front matter of the committed ``sample_docs/``
corpus (verified against the files) so the lint's client-isolation check runs
against a real ownership map.
"""

from __future__ import annotations

from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent.parent
FIXTURE_WIKI = _TESTS_DIR / "fixtures" / "wiki_fixture"
PROPOSALS_DIR = _TESTS_DIR / "fixtures" / "wiki_proposals"

# document_id -> owning client (None = global), from sample_docs/ front matter.
DOC_CLIENTS: dict[str, str | None] = {
    "alpha_trading_bookkeeping_sop_2026": "Alpha Trading Co.",
    "beta_catering_invoice_rule_2026": "Beta Catering Ltd.",
    "reimbursement_policy_2024": None,
    "reimbursement_policy_2026": None,
    "vat_policy_note_2025": None,
    "monthly_bookkeeping_checklist_2026": None,
}
KNOWN_DOC_IDS = set(DOC_CLIENTS)
