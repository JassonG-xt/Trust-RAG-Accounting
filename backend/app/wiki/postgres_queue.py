"""Postgres-backed wiki proposal review queue (Phase 10E, defect A).

The wiki proposal store moves off a single local JSON file onto two tables —
``wiki_proposals`` and ``wiki_proposal_actions`` — so the REST review queue and
the ``trustrag-wiki`` CLI share one durable source. Every ``act()`` performs the
whole state transition (lock → state-machine check → action write → status
update) inside one transaction; a concurrent reviewer on the same proposal
blocks behind ``FOR UPDATE``, then reads the *fresh* status and gets the normal
:class:`InvalidReviewTransitionError` instead of silently dropping a decision.

A repository is bound to exactly one ``tenant_id`` at construction; ``get`` /
``list`` never cross that boundary and ``enqueue`` is idempotent on
``(tenant_id, proposal_id)``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Engine, and_, insert, select, update

from ..persistence.schema import wiki_proposal_actions, wiki_proposals
from ..review.state_machine import apply_review_action
from .models import WikiUpdateProposal
from .review_queue import WikiProposalRecord


class WikiProposalConflictError(RuntimeError):
    """Raised when enqueueing a payload that collides with an existing record."""


class PostgresWikiProposalRepository:
    """Tenant-scoped durable implementation of ``WikiProposalStore``."""

    def __init__(self, engine: Engine, *, tenant_id: str) -> None:
        if not tenant_id.strip():
            raise ValueError("tenant_id must not be empty")
        self._engine = engine
        self._tenant_id = tenant_id

    # -- queue ops -------------------------------------------------------

    def enqueue(
        self,
        proposal: WikiUpdateProposal,
        *,
        created_at: str,
        tenant_id: str | None = None,
    ) -> str:
        """Stage a proposal as ``pending`` (idempotent per tenant+id).

        Re-enqueueing an identical proposal is a no-op returning the same id.
        Re-enqueueing with a *different* payload raises
        :class:`WikiProposalConflictError` — a record is never silently
        overwritten.
        """

        values = {
            "tenant_id": self._tenant_id,
            "proposal_id": proposal.proposal_id,
            "risk": proposal.risk,
            "created_at": created_at,
            "proposal": proposal.model_dump(mode="json"),
        }
        with self._engine.begin() as connection:
            existing = connection.execute(
                select(wiki_proposals).where(
                    and_(
                        wiki_proposals.c.tenant_id == self._tenant_id,
                        wiki_proposals.c.proposal_id == proposal.proposal_id,
                    )
                )
            ).mappings().one_or_none()
            if existing is not None:
                if (
                    existing["risk"] != values["risk"]
                    or existing["created_at"] != values["created_at"]
                    or existing["proposal"] != values["proposal"]
                ):
                    raise WikiProposalConflictError(
                        f"proposal {proposal.proposal_id!r} already queued for "
                        f"tenant {self._tenant_id!r} with a different payload"
                    )
                return proposal.proposal_id
            connection.execute(
                insert(wiki_proposals).values(status="pending", **values)
            )
        return proposal.proposal_id

    def list(self, *, tenant_id: str | None = None) -> list[WikiProposalRecord]:
        """Records for this tenant, risk-sorted (sensitive first) then created."""
        statement = (
            select(wiki_proposals.c.proposal_id)
            .where(wiki_proposals.c.tenant_id == self._tenant_id)
            .order_by(
                wiki_proposals.c.risk != "sensitive",
                wiki_proposals.c.created_at,
                wiki_proposals.c.proposal_id,
            )
        )
        with self._engine.connect() as connection:
            proposal_ids = list(connection.execute(statement).scalars())
        return [self._load_record(proposal_id) for proposal_id in proposal_ids]

    def get(self, proposal_id: str) -> WikiProposalRecord:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(wiki_proposals).where(
                    and_(
                        wiki_proposals.c.tenant_id == self._tenant_id,
                        wiki_proposals.c.proposal_id == proposal_id,
                    )
                )
            ).mappings().one_or_none()
            if row is None:
                raise KeyError(f"no such proposal: {proposal_id}")
        return WikiProposalRecord(
            proposal_id=row["proposal_id"],
            status=row["status"],
            risk=row["risk"],
            created_at=row["created_at"],
            tenant_id=self._tenant_id,
            proposal=WikiUpdateProposal.model_validate(row["proposal"]),
            actions=self._load_actions(proposal_id=row["proposal_id"]),
        )

    def act(self, proposal_id: str, action_type: str, *, at: str) -> str:
        """Transition the proposal's review status inside one transaction.

        The proposal row is locked ``FOR UPDATE``; the state machine is checked
        against the freshly read status, the action is written, and the status
        updated — all or nothing. A concurrent reviewer whose transaction runs
        later re-reads the committed status and receives
        ``InvalidReviewTransitionError`` from the shared FSM.
        """

        with self._engine.begin() as connection:
            current_status = connection.execute(
                select(wiki_proposals.c.status)
                .where(
                    and_(
                        wiki_proposals.c.tenant_id == self._tenant_id,
                        wiki_proposals.c.proposal_id == proposal_id,
                    )
                )
                .with_for_update()
            ).scalar_one_or_none()
            if current_status is None:
                raise KeyError(f"no such proposal: {proposal_id}")
            new_status = apply_review_action(current_status, action_type)
            action_id = f"act-{uuid.uuid4().hex}"
            connection.execute(
                insert(wiki_proposal_actions).values(
                    tenant_id=self._tenant_id,
                    action_id=action_id,
                    proposal_id=proposal_id,
                    action_type=action_type,
                    previous_status=current_status,
                    new_status=new_status,
                    created_at=at,
                    payload={
                        "action_type": action_type,
                        "at": at,
                        "new_status": new_status,
                    },
                )
            )
            connection.execute(
                update(wiki_proposals)
                .where(
                    and_(
                        wiki_proposals.c.tenant_id == self._tenant_id,
                        wiki_proposals.c.proposal_id == proposal_id,
                    )
                )
                .values(status=new_status)
            )
        return new_status

    # -- reconstruction ---------------------------------------------------

    def _load_record(self, proposal_id: str) -> WikiProposalRecord:
        with self._engine.connect() as connection:
            row = connection.execute(
                select(wiki_proposals).where(
                    and_(
                        wiki_proposals.c.tenant_id == self._tenant_id,
                        wiki_proposals.c.proposal_id == proposal_id,
                    )
                )
            ).mappings().one()
        return WikiProposalRecord(
            proposal_id=row["proposal_id"],
            status=row["status"],
            risk=row["risk"],
            created_at=row["created_at"],
            tenant_id=self._tenant_id,
            proposal=WikiUpdateProposal.model_validate(row["proposal"]),
            actions=self._load_actions(proposal_id=row["proposal_id"]),
        )

    def _load_actions(self, *, proposal_id: str) -> list[dict]:
        statement = (
            select(wiki_proposal_actions.c.payload)
            .where(
                and_(
                    wiki_proposal_actions.c.tenant_id == self._tenant_id,
                    wiki_proposal_actions.c.proposal_id == proposal_id,
                )
            )
            .order_by(
                wiki_proposal_actions.c.created_at,
                wiki_proposal_actions.c.action_id,
            )
        )
        with self._engine.connect() as connection:
            return list(connection.execute(statement).scalars())


__all__ = [
    "PostgresWikiProposalRepository",
    "WikiProposalConflictError",
]
