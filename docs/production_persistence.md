# Production Persistence

TrustRAG keeps filesystem implementations as the zero-dependency development
default and provides Postgres and S3-compatible implementations behind the
same application seams.

## Initial rollout

1. Install `pip install -e '.[production]'`.
2. Set `DATABASE_URL` and run `alembic upgrade head`.
3. Import legacy JSON/JSONL with `trustrag-import-legacy`.
4. Compare document, version, chunk, checkpoint, and action counts.
5. Set `TRUSTRAG_STORAGE_BACKEND=postgres` and a stable
   `TRUSTRAG_TENANT_ID`.
6. Keep the legacy files read-only for one release before archiving them.

The import command is idempotent. Review actions are inserted in historical
order and their `previous_status` must match the stored state projection.

## Wiki proposal queue rollout (defect A)

The wiki proposal review queue also migrates from the local JSON store to
Postgres so the REST `/v1/wiki/proposals*` endpoints and the `trustrag-wiki`
CLI share the same durable proposals and review actions. Rollout order:

1. Run `alembic upgrade head` to create `wiki_proposals` and
   `wiki_proposal_actions`.
2. Import the legacy queue with `--wiki-proposals data/wiki_proposals.json`
   (unused for the old command when the flag is omitted).
3. Compare proposal and wiki-action counts before/after the import, and
   re-run the import to verify it is idempotent (no duplicates).
4. Set `TRUSTRAG_STORAGE_BACKEND=postgres`.
5. Set `WIKI_ENABLED=true` — the proposal list now returns `enabled=true`.
6. Keep `data/wiki_proposals.json` read-only for one release before
   archiving it.
7. On rollback, switching back to `local` restores the JSON queue and does
   **not** copy newer Postgres proposals/actions back into the JSON file.

Concurrent reviewers are safe: `act()` locks the proposal row (`FOR UPDATE`),
validates the shared review state machine against the fresh status, writes the
action and updates the status in one transaction, so a second reviewer against
the same proposal re-reads the latest status and gets an explicit invalid
transition error instead of silently dropping their action.

## Consistency and rollback

- Postgres review writes lock the checkpoint row and reject stale status
  transitions instead of silently accepting concurrent reviewer decisions.
- Every production row is scoped by `tenant_id`; the business `client` field
  remains document metadata and is not a security boundary.
- Switching `TRUSTRAG_STORAGE_BACKEND` back to `local` restores the local demo
  implementation. It does not copy newer Postgres records back into JSONL.
- Original files stored through the S3 adapter use checksum-addressed keys, so
  identical uploads resolve to the same immutable URI.

Do not run `metadata.create_all()` in production. Alembic is the authoritative
schema migration path.
