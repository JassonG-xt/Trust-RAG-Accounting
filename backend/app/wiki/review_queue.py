"""Wiki proposal review queue — every proposal is reviewed, none auto-applies.

Reuses the Phase 7B review **state machine** (``apply_review_action``) so wiki
updates share the exact transition semantics of the RAG review queue
(approve / reject / request_changes / rewrite_note / resolve / reopen) without
touching the RAG checkpoint store. A proposal enters ``pending``; the applier
runs only when an ``approve`` action transitions it to ``approved`` — there is
no auto-apply path.

Persistence is a small JSON file (proposal_id -> record) under gitignored
``data/`` by default; risk-sorted for the reviewer (``sensitive`` before
``low``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from ..review.state_machine import apply_review_action
from .apply import apply_proposal
from .ingest import derive_doc_maps, derive_source_doc_types
from .models import ApplyResult, WikiUpdateProposal


class WikiProposalRecord(BaseModel):
    proposal_id: str
    status: str = "pending"
    risk: str = "low"
    created_at: str
    tenant_id: str | None = None
    proposal: WikiUpdateProposal
    actions: list[dict] = Field(default_factory=list)


class WikiProposalsResponse(BaseModel):
    """Response shape for ``GET /v1/wiki/proposals``.

    ``enabled=false`` (never a 404) when the wiki feature is off or the
    proposal store is unavailable — same contract as the RAG review queue.
    Entries are the current tenant's proposals only.
    """

    enabled: bool
    count: int = 0
    total: int = 0
    entries: list[WikiProposalRecord] = Field(default_factory=list)


class WikiProposalActionRequest(BaseModel):
    """Body for ``POST /v1/wiki/proposals/{id}/actions``."""

    action_type: str
    note: str | None = None


class WikiProposalActionResponse(BaseModel):
    """Response shape for ``POST /v1/wiki/proposals/{id}/actions``.

    ``status`` is the proposal's new review status (``approve`` moves it to
    ``approved`` and runs the applier).
    """

    proposal_id: str
    status: str


class WikiProposalStore(Protocol):
    """Persistence seam shared by the local JSON queue and Postgres.

    Both implementations keep the same review surface (``enqueue`` /
    ``list`` / ``get`` / ``act``) so the REST layer, the CLI and the applier
    observe identical semantics — the stored proposal records, however, are
    tenant-scoped by the store that owns them.
    """

    def enqueue(
        self,
        proposal: WikiUpdateProposal,
        *,
        created_at: str,
        tenant_id: str | None = None,
    ) -> str: ...

    def list(self, *, tenant_id: str | None = None) -> list[WikiProposalRecord]: ...

    def get(self, proposal_id: str) -> WikiProposalRecord: ...

    def act(self, proposal_id: str, action_type: str, *, at: str) -> str: ...


class WikiReviewQueue:
    """A local, file-backed queue of staged wiki proposals."""

    def __init__(self, store_path: Path | str):
        self.store_path = Path(store_path)

    # -- persistence -----------------------------------------------------

    def _load(self) -> dict[str, WikiProposalRecord]:
        if not self.store_path.exists():
            return {}
        data = json.loads(self.store_path.read_text(encoding="utf-8"))
        return {pid: WikiProposalRecord(**rec) for pid, rec in data.items()}

    def _save(self, records: dict[str, WikiProposalRecord]) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {pid: rec.model_dump() for pid, rec in records.items()}
        self.store_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    # -- queue ops -------------------------------------------------------

    def enqueue(
        self, proposal: WikiUpdateProposal, *, created_at: str, tenant_id: str | None = None
    ) -> str:
        """Stage a proposal as ``pending``. Writes nothing to the wiki."""

        records = self._load()
        records[proposal.proposal_id] = WikiProposalRecord(
            proposal_id=proposal.proposal_id,
            status="pending",
            risk=proposal.risk,
            created_at=created_at,
            tenant_id=tenant_id,
            proposal=proposal,
        )
        self._save(records)
        return proposal.proposal_id

    def list(self, *, tenant_id: str | None = None) -> list[WikiProposalRecord]:
        """Records, risk-sorted (sensitive first) then by creation time.

        ``tenant_id=None`` returns every tenant's records (the pre-10D
        behavior); a tenant filters the queue to that tenant's proposals.
        """

        records = list(self._load().values())
        if tenant_id is not None:
            records = [r for r in records if r.tenant_id == tenant_id]
        return sorted(records, key=lambda r: (r.risk != "sensitive", r.created_at))

    def get(self, proposal_id: str) -> WikiProposalRecord:
        records = self._load()
        if proposal_id not in records:
            raise KeyError(f"no such proposal: {proposal_id}")
        return records[proposal_id]

    def act(self, proposal_id: str, action_type: str, *, at: str) -> str:
        """Apply a review action; returns the new status.

        Raises ``InvalidReviewTransitionError`` (from the shared state machine)
        on a disallowed transition, and ``KeyError`` (same message as
        :meth:`get`) for an unknown proposal id.
        """

        records = self._load()
        if proposal_id not in records:
            raise KeyError(f"no such proposal: {proposal_id}")
        rec = records[proposal_id]
        new_status = apply_review_action(rec.status, action_type)
        rec.status = new_status
        rec.actions.append({"action_type": action_type, "at": at, "new_status": new_status})
        self._save(records)
        return new_status


def approve_and_apply(
    queue: WikiProposalStore,
    proposal_id: str,
    wiki_dir: Path | str,
    *,
    at: str,
    repository,
    pages_out=None,
    chunks_out=None,
    applied_ledger_path=None,
    force: bool = False,
) -> ApplyResult:
    """Approve a proposal and run the applier (the only approve→write path).

    The client-isolation / unknown-source lint gates are armed from
    ``repository`` here — a write path can never ship them unarmed (the caller
    cannot pass ``doc_clients=None`` through this function).

    An already-``approved`` record skips the state transition (the state
    machine has no ``approved→approve`` retry) and reruns the applier, which
    returns ``status="noop"`` via its idempotency ledger when nothing changed —
    this also lets a reviewer retry an approve after a partial apply failure
    (Phase 10B P1-6 divergence: approved persisted, nothing on disk).
    """

    known_doc_ids, doc_clients = derive_doc_maps(repository)
    source_doc_types = derive_source_doc_types(repository)
    rec = queue.get(proposal_id)
    if rec.status != "approved":
        status = queue.act(proposal_id, "approve", at=at)
        if status != "approved":
            raise RuntimeError(f"proposal {proposal_id} is {status}, not approved")
        rec = queue.get(proposal_id)
    return apply_proposal(
        rec.proposal,
        wiki_dir,
        pages_out=pages_out,
        chunks_out=chunks_out,
        applied_ledger_path=applied_ledger_path,
        known_doc_ids=known_doc_ids,
        doc_clients=doc_clients,
        source_doc_types=source_doc_types,
        force=force,
    )
